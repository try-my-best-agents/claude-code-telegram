"""Claude Code Python SDK integration.

Features:
- Native Claude Code SDK integration
- Async streaming support
- Tool execution management
- Session persistence
"""

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog
from claude_code_sdk import query, ClaudeCodeOptions, Message
from claude_code_sdk.types import AssistantMessage, UserMessage, ResultMessage

from ..config.settings import Settings
from .exceptions import (
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

logger = structlog.get_logger()


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

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
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict]] = None
    metadata: Optional[Dict] = None


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(self, config: Settings):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        
        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK."""
        start_time = asyncio.get_event_loop().time()
        
        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Build Claude Code options
            options = ClaudeCodeOptions(
                max_turns=self.config.claude_max_turns,
                cwd=str(working_directory),
                allowed_tools=self.config.claude_allowed_tools,
            )
            
            # Collect messages
            messages = []
            cost = 0.0
            tools_used = []
            
            # Execute with streaming and timeout
            await asyncio.wait_for(
                self._execute_query_with_streaming(
                    prompt, options, messages, stream_callback
                ),
                timeout=self.config.claude_timeout_seconds
            )
            
            # Extract cost and tools from result message
            cost = 0.0
            tools_used = []
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, 'total_cost_usd', 0.0) or 0.0
                    tools_used = self._extract_tools_from_messages(messages)
                    break
            
            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            
            # Get or create session ID
            final_session_id = session_id or str(uuid.uuid4())
            
            # Update session
            self._update_session(final_session_id, messages)
            
            return ClaudeResponse(
                content=self._extract_content_from_messages(messages),
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len([m for m in messages if isinstance(m, (UserMessage, AssistantMessage))]),
                tools_used=tools_used,
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=self.config.claude_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            )

        except Exception as e:
            logger.error("Claude SDK command failed", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

    async def _execute_query_with_streaming(
        self,
        prompt: str,
        options,
        messages: List,
        stream_callback: Optional[Callable]
    ) -> None:
        """Execute query with streaming and collect messages."""
        async for message in query(prompt=prompt, options=options):
            messages.append(message)
            
            # Handle streaming callback
            if stream_callback:
                await self._handle_stream_message(message, stream_callback)

    async def _handle_stream_message(
        self, message: Message, stream_callback: Callable[[StreamUpdate], None]
    ) -> None:
        """Handle streaming message from claude-code-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, 'content', [])
                if content and isinstance(content, list):
                    # Extract text from TextBlock objects
                    text_parts = []
                    for block in content:
                        if hasattr(block, 'text'):
                            text_parts.append(block.text)
                    if text_parts:
                        update = StreamUpdate(
                            type="assistant",
                            content="\n".join(text_parts),
                        )
                        await stream_callback(update)
                elif content:
                    # Fallback for non-list content
                    update = StreamUpdate(
                        type="assistant",
                        content=str(content),
                    )
                    await stream_callback(update)
                
                # Check for tool calls (if available in the message structure)
                # Note: This depends on the actual claude-code-sdk message structure
                
            elif isinstance(message, UserMessage):
                content = getattr(message, 'content', '')
                if content:
                    update = StreamUpdate(
                        type="user",
                        content=content,
                    )
                    await stream_callback(update)
                    
        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    def _extract_content_from_messages(self, messages: List[Message]) -> str:
        """Extract content from message list."""
        content_parts = []
        
        for message in messages:
            if isinstance(message, AssistantMessage):
                content = getattr(message, 'content', [])
                if content and isinstance(content, list):
                    # Extract text from TextBlock objects
                    for block in content:
                        if hasattr(block, 'text'):
                            content_parts.append(block.text)
                elif content:
                    # Fallback for non-list content
                    content_parts.append(str(content))
        
        return "\n".join(content_parts)

    def _extract_tools_from_messages(self, messages: List[Message]) -> List[Dict[str, Any]]:
        """Extract tools used from message list."""
        tools_used = []
        current_time = asyncio.get_event_loop().time()
        
        for message in messages:
            # This depends on the actual claude-code-sdk message structure
            # We'll extract tool information if available
            if hasattr(message, 'tool_calls'):
                for tool_call in message.tool_calls:
                    tools_used.append({
                        "name": getattr(tool_call, 'name', 'unknown'),
                        "timestamp": current_time,
                    })
        
        return tools_used

    def _update_session(self, session_id: str, messages: List[Message]) -> None:
        """Update session data."""
        if session_id not in self.active_sessions:
            self.active_sessions[session_id] = {
                "messages": [],
                "created_at": asyncio.get_event_loop().time(),
            }
        
        session_data = self.active_sessions[session_id]
        session_data["messages"] = messages
        session_data["last_used"] = asyncio.get_event_loop().time()

    async def kill_all_processes(self) -> None:
        """Kill all active processes (no-op for SDK)."""
        logger.info("Clearing active SDK sessions", count=len(self.active_sessions))
        self.active_sessions.clear()

    def get_active_process_count(self) -> int:
        """Get number of active sessions."""
        return len(self.active_sessions)