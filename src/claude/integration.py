"""Claude Code subprocess management.

Features:
- Async subprocess execution
- Stream handling
- Timeout management
- Error recovery
"""

import asyncio
import json
import uuid
from asyncio.subprocess import Process
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog

from ..config.settings import Settings
from .exceptions import (
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

logger = structlog.get_logger()


@dataclass
class ClaudeResponse:
    """Response from Claude Code."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class StreamUpdate:
    """Streaming update from Claude."""

    type: str  # 'assistant', 'user', 'system', 'result'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None


class ClaudeProcessManager:
    """Manage Claude Code subprocess execution."""

    def __init__(self, config: Settings):
        """Initialize process manager with configuration."""
        self.config = config
        self.active_processes: Dict[str, Process] = {}

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command."""
        # Build command
        cmd = self._build_command(prompt, session_id, continue_session)

        # Create process ID for tracking
        process_id = str(uuid.uuid4())

        logger.info(
            "Starting Claude Code process",
            process_id=process_id,
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Start process
            process = await self._start_process(cmd, working_directory)
            self.active_processes[process_id] = process

            # Handle output with timeout
            result = await asyncio.wait_for(
                self._handle_process_output(process, stream_callback),
                timeout=self.config.claude_timeout_seconds,
            )

            logger.info(
                "Claude Code process completed successfully",
                process_id=process_id,
                cost=result.cost,
                duration_ms=result.duration_ms,
            )

            return result

        except asyncio.TimeoutError:
            # Kill process on timeout
            if process_id in self.active_processes:
                self.active_processes[process_id].kill()
                await self.active_processes[process_id].wait()

            logger.error(
                "Claude Code process timed out",
                process_id=process_id,
                timeout_seconds=self.config.claude_timeout_seconds,
            )

            raise ClaudeTimeoutError(
                f"Claude Code timed out after {self.config.claude_timeout_seconds}s"
            )

        except Exception as e:
            logger.error(
                "Claude Code process failed",
                process_id=process_id,
                error=str(e),
            )
            raise

        finally:
            # Clean up
            if process_id in self.active_processes:
                del self.active_processes[process_id]

    def _build_command(
        self, prompt: str, session_id: Optional[str], continue_session: bool
    ) -> List[str]:
        """Build Claude Code command with arguments."""
        cmd = [self.config.claude_binary_path or "claude"]

        if continue_session and not prompt:
            # Continue existing session without new prompt
            cmd.extend(["--continue"])
            if session_id:
                cmd.extend(["--resume", session_id])
        elif session_id and prompt and continue_session:
            # Follow-up message in existing session - use resume with new prompt
            cmd.extend(["--resume", session_id, "-p", prompt])
        elif prompt:
            # New session with prompt (including new sessions with session_id)
            cmd.extend(["-p", prompt])
        else:
            # This shouldn't happen, but fallback to new session
            cmd.extend(["-p", ""])

        # Always use streaming JSON for real-time updates
        cmd.extend(["--output-format", "stream-json"])

        # stream-json requires --verbose when using --print mode
        cmd.extend(["--verbose"])

        # Add safety limits
        cmd.extend(["--max-turns", str(self.config.claude_max_turns)])

        # Add allowed tools if configured
        if (
            hasattr(self.config, "claude_allowed_tools")
            and self.config.claude_allowed_tools
        ):
            cmd.extend(["--allowedTools", ",".join(self.config.claude_allowed_tools)])

        logger.debug("Built Claude Code command", command=cmd)
        return cmd

    async def _start_process(self, cmd: List[str], cwd: Path) -> Process:
        """Start Claude Code subprocess."""
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            # Limit memory usage
            limit=1024 * 1024 * 512,  # 512MB
        )

    async def _handle_process_output(
        self, process: Process, stream_callback: Optional[Callable]
    ) -> ClaudeResponse:
        """Handle streaming output from Claude Code."""
        messages = []
        result = None

        async for line in self._read_stream(process.stdout):
            try:
                msg = json.loads(line)
                messages.append(msg)

                # Create stream update
                update = self._parse_stream_message(msg)
                if update and stream_callback:
                    try:
                        await stream_callback(update)
                    except Exception as e:
                        logger.warning("Stream callback failed", error=str(e))

                # Check for final result
                if msg.get("type") == "result":
                    result = msg

            except json.JSONDecodeError:
                logger.warning("Failed to parse JSON line", line=line)
                continue

        # Wait for process to complete
        return_code = await process.wait()

        if return_code != 0:
            stderr = await process.stderr.read()
            error_msg = stderr.decode("utf-8", errors="replace")
            logger.error(
                "Claude Code process failed",
                return_code=return_code,
                stderr=error_msg,
            )

            # Check for specific error types
            if "usage limit reached" in error_msg.lower():
                # Extract reset time if available
                import re

                time_match = re.search(
                    r"reset at (\d+[apm]+)", error_msg, re.IGNORECASE
                )
                timezone_match = re.search(r"\(([^)]+)\)", error_msg)

                reset_time = time_match.group(1) if time_match else "later"
                timezone = timezone_match.group(1) if timezone_match else ""

                user_friendly_msg = (
                    f"⏱️ **Claude AI Usage Limit Reached**\n\n"
                    f"You've reached your Claude AI usage limit for this period.\n\n"
                    f"**When will it reset?**\n"
                    f"Your limit will reset at **{reset_time}**"
                    f"{f' ({timezone})' if timezone else ''}\n\n"
                    f"**What you can do:**\n"
                    f"• Wait for the limit to reset automatically\n"
                    f"• Try again after the reset time\n"
                    f"• Use simpler requests that require less processing\n"
                    f"• Contact support if you need a higher limit"
                )

                raise ClaudeProcessError(user_friendly_msg)

            # Generic error handling for other cases
            raise ClaudeProcessError(
                f"Claude Code exited with code {return_code}: {error_msg}"
            )

        if not result:
            logger.error("No result message received from Claude Code")
            raise ClaudeParsingError("No result message received from Claude Code")

        return self._parse_result(result, messages)

    async def _read_stream(self, stream) -> AsyncIterator[str]:
        """Read lines from stream."""
        while True:
            line = await stream.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace").strip()

    def _parse_stream_message(self, msg: Dict) -> Optional[StreamUpdate]:
        """Parse streaming message into update."""
        msg_type = msg.get("type")

        if msg_type == "assistant":
            # Extract content and tool calls
            message = msg.get("message", {})
            content_blocks = message.get("content", [])

            # Get text content
            text_content = []
            tool_calls = []

            for block in content_blocks:
                if block.get("type") == "text":
                    text_content.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        {"name": block.get("name"), "input": block.get("input", {})}
                    )

            return StreamUpdate(
                type="assistant",
                content="\n".join(text_content) if text_content else None,
                tool_calls=tool_calls if tool_calls else None,
            )

        elif msg_type == "system" and msg.get("subtype") == "init":
            # Initial system message with available tools
            return StreamUpdate(
                type="system",
                metadata={
                    "tools": msg.get("tools", []),
                    "mcp_servers": msg.get("mcp_servers", []),
                },
            )

        return None

    def _parse_result(self, result: Dict, messages: List[Dict]) -> ClaudeResponse:
        """Parse final result message."""
        # Extract tools used from messages
        tools_used = []
        for msg in messages:
            if msg.get("type") == "assistant":
                message = msg.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "tool_use":
                        tools_used.append(
                            {
                                "name": block.get("name"),
                                "timestamp": msg.get("timestamp"),
                            }
                        )

        return ClaudeResponse(
            content=result.get("result", ""),
            session_id=result.get("session_id", ""),
            cost=result.get("cost_usd", 0.0),
            duration_ms=result.get("duration_ms", 0),
            num_turns=result.get("num_turns", 0),
            is_error=result.get("is_error", False),
            error_type=result.get("subtype") if result.get("is_error") else None,
            tools_used=tools_used,
        )

    async def kill_all_processes(self) -> None:
        """Kill all active processes."""
        logger.info(
            "Killing all active Claude processes", count=len(self.active_processes)
        )

        for process_id, process in self.active_processes.items():
            try:
                process.kill()
                await process.wait()
                logger.info("Killed Claude process", process_id=process_id)
            except Exception as e:
                logger.warning(
                    "Failed to kill process", process_id=process_id, error=str(e)
                )

        self.active_processes.clear()

    def get_active_process_count(self) -> int:
        """Get number of active processes."""
        return len(self.active_processes)
