"""Format bot responses for optimal display."""

import re
from dataclasses import dataclass
from typing import Any, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...config.settings import Settings


@dataclass
class FormattedMessage:
    """Represents a formatted message for Telegram."""

    text: str
    parse_mode: str = "Markdown"
    reply_markup: Optional[InlineKeyboardMarkup] = None

    def __len__(self) -> int:
        """Return length of message text."""
        return len(self.text)


class ResponseFormatter:
    """Format Claude responses for Telegram display."""

    def __init__(self, settings: Settings):
        """Initialize formatter with settings."""
        self.settings = settings
        self.max_message_length = 4000  # Telegram limit is 4096, leave some buffer
        self.max_code_block_length = 3000  # Max length for code blocks

    def format_claude_response(self, text: str) -> List[FormattedMessage]:
        """Format Claude response into one or more Telegram messages."""
        # Clean and prepare text
        text = self._clean_text(text)

        # Handle code blocks specially
        text = self._format_code_blocks(text)

        # Split into messages if too long
        messages = self._split_message(text)

        # Add quick actions to the last message if enabled
        if self.settings.enable_quick_actions and messages:
            messages[-1].reply_markup = self._get_quick_actions_keyboard()

        return messages

    def format_error_message(
        self, error: str, error_type: str = "Error"
    ) -> FormattedMessage:
        """Format error message with appropriate styling."""
        icon = {
            "Error": "❌",
            "Warning": "⚠️",
            "Info": "ℹ️",
            "Security": "🛡️",
            "Rate Limit": "⏱️",
        }.get(error_type, "❌")

        text = f"{icon} **{error_type}**\n\n{error}"

        return FormattedMessage(text, parse_mode="Markdown")

    def format_success_message(
        self, message: str, title: str = "Success"
    ) -> FormattedMessage:
        """Format success message with appropriate styling."""
        text = f"✅ **{title}**\n\n{message}"
        return FormattedMessage(text, parse_mode="Markdown")

    def format_info_message(
        self, message: str, title: str = "Info"
    ) -> FormattedMessage:
        """Format info message with appropriate styling."""
        text = f"ℹ️ **{title}**\n\n{message}"
        return FormattedMessage(text, parse_mode="Markdown")

    def format_code_output(
        self, output: str, language: str = "", title: str = "Output"
    ) -> List[FormattedMessage]:
        """Format code output with syntax highlighting."""
        if not output.strip():
            return [FormattedMessage(f"📄 **{title}**\n\n_(empty output)_")]

        # Add language hint if provided
        code_block = (
            f"```{language}\n{output}\n```" if language else f"```\n{output}\n```"
        )

        # Check if the code block is too long
        if len(code_block) > self.max_code_block_length:
            # Truncate and add notice
            truncated = output[: self.max_code_block_length - 100]
            code_block = f"```{language}\n{truncated}\n... (output truncated)\n```"

        text = f"📄 **{title}**\n\n{code_block}"

        return self._split_message(text)

    def format_file_list(
        self, files: List[str], directory: str = ""
    ) -> FormattedMessage:
        """Format file listing with appropriate icons."""
        if not files:
            text = f"📂 **{directory}**\n\n_(empty directory)_"
        else:
            file_lines = []
            for file in files[:50]:  # Limit to 50 items
                if file.endswith("/"):
                    file_lines.append(f"📁 {file}")
                else:
                    file_lines.append(f"📄 {file}")

            file_text = "\n".join(file_lines)
            if len(files) > 50:
                file_text += f"\n\n_... and {len(files) - 50} more items_"

            text = f"📂 **{directory}**\n\n{file_text}"

        return FormattedMessage(text, parse_mode="Markdown")

    def format_progress_message(
        self, message: str, percentage: Optional[float] = None
    ) -> FormattedMessage:
        """Format progress message with optional progress bar."""
        if percentage is not None:
            # Create simple progress bar
            filled = int(percentage / 10)
            empty = 10 - filled
            progress_bar = "▓" * filled + "░" * empty
            text = f"🔄 **{message}**\n\n{progress_bar} {percentage:.0f}%"
        else:
            text = f"🔄 **{message}**"

        return FormattedMessage(text, parse_mode="Markdown")

    def _clean_text(self, text: str) -> str:
        """Clean text for Telegram display."""
        # Remove excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)

        # Escape special Markdown characters (but preserve intentional formatting)
        # Be careful not to escape characters inside code blocks
        text = self._escape_markdown_outside_code(text)

        return text.strip()

    def _escape_markdown_outside_code(self, text: str) -> str:
        """Escape Markdown characters outside of code blocks."""
        # This is a simplified approach - in practice, you might want more sophisticated parsing
        parts = []
        in_code_block = False
        in_inline_code = False

        lines = text.split("\n")
        for line in lines:
            if line.strip() == "```":
                in_code_block = not in_code_block
                parts.append(line)
            elif in_code_block:
                parts.append(line)
            else:
                # Handle inline code
                line_parts = line.split("`")
                for i, part in enumerate(line_parts):
                    if i % 2 == 0:  # Outside inline code
                        # Escape special characters
                        part = part.replace("_", r"\_").replace("*", r"\*")
                    line_parts[i] = part
                parts.append("`".join(line_parts))

        return "\n".join(parts)

    def _format_code_blocks(self, text: str) -> str:
        """Ensure code blocks are properly formatted for Telegram."""
        # Handle triple backticks with language specification
        pattern = r"```(\w+)?\n(.*?)```"

        def replace_code_block(match):
            lang = match.group(1) or ""
            code = match.group(2)

            # Telegram doesn't support language hints, but we can add them as comments
            if lang and lang.lower() not in ["text", "plain"]:
                # Add language as a comment at the top
                code = f"# {lang}\n{code}"

            # Ensure code block doesn't exceed length limits
            if len(code) > self.max_code_block_length:
                code = code[: self.max_code_block_length - 50] + "\n... (truncated)"

            return f"```\n{code}\n```"

        return re.sub(pattern, replace_code_block, text, flags=re.DOTALL)

    def _split_message(self, text: str) -> List[FormattedMessage]:
        """Split long messages while preserving formatting."""
        if len(text) <= self.max_message_length:
            return [FormattedMessage(text)]

        messages = []
        current_lines = []
        current_length = 0
        in_code_block = False

        lines = text.split("\n")

        for line in lines:
            line_length = len(line) + 1  # +1 for newline

            # Check for code block markers
            if line.strip() == "```":
                in_code_block = not in_code_block

            # If this is a very long line that exceeds limit by itself, split it
            if line_length > self.max_message_length:
                # Split the line into chunks
                chunks = []
                for i in range(0, len(line), self.max_message_length - 100):
                    chunks.append(line[i : i + self.max_message_length - 100])

                for chunk in chunks:
                    chunk_length = len(chunk) + 1

                    if (
                        current_length + chunk_length > self.max_message_length
                        and current_lines
                    ):
                        # Save current message
                        if in_code_block:
                            current_lines.append("```")
                        messages.append(FormattedMessage("\n".join(current_lines)))

                        # Start new message
                        current_lines = []
                        current_length = 0
                        if in_code_block:
                            current_lines.append("```")
                            current_length = 4

                    current_lines.append(chunk)
                    current_length += chunk_length
                continue

            # Check if adding this line would exceed the limit
            if current_length + line_length > self.max_message_length and current_lines:
                # Close code block if we're in one
                if in_code_block:
                    current_lines.append("```")

                # Save current message
                messages.append(FormattedMessage("\n".join(current_lines)))

                # Start new message
                current_lines = []
                current_length = 0

                # Reopen code block if needed
                if in_code_block:
                    current_lines.append("```")
                    current_length = 4  # Length of '```\n'

            current_lines.append(line)
            current_length += line_length

        # Add remaining content
        if current_lines:
            # Close code block if needed
            if in_code_block:
                current_lines.append("```")
            messages.append(FormattedMessage("\n".join(current_lines)))

        return messages

    def _get_quick_actions_keyboard(self) -> InlineKeyboardMarkup:
        """Get quick actions inline keyboard."""
        keyboard = [
            [
                InlineKeyboardButton("🧪 Test", callback_data="quick:test"),
                InlineKeyboardButton("📦 Install", callback_data="quick:install"),
                InlineKeyboardButton("🎨 Format", callback_data="quick:format"),
            ],
            [
                InlineKeyboardButton("🔍 Find TODOs", callback_data="quick:find_todos"),
                InlineKeyboardButton("🔨 Build", callback_data="quick:build"),
                InlineKeyboardButton("📊 Git Status", callback_data="quick:git_status"),
            ],
        ]

        return InlineKeyboardMarkup(keyboard)

    def create_confirmation_keyboard(
        self, confirm_data: str, cancel_data: str = "confirm:no"
    ) -> InlineKeyboardMarkup:
        """Create a confirmation keyboard."""
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes", callback_data=confirm_data),
                InlineKeyboardButton("❌ No", callback_data=cancel_data),
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    def create_navigation_keyboard(self, options: List[tuple]) -> InlineKeyboardMarkup:
        """Create navigation keyboard from options list.

        Args:
            options: List of (text, callback_data) tuples
        """
        keyboard = []
        current_row = []

        for text, callback_data in options:
            current_row.append(InlineKeyboardButton(text, callback_data=callback_data))

            # Create rows of 2 buttons
            if len(current_row) == 2:
                keyboard.append(current_row)
                current_row = []

        # Add remaining button if any
        if current_row:
            keyboard.append(current_row)

        return InlineKeyboardMarkup(keyboard)


class ProgressIndicator:
    """Helper for creating progress indicators."""

    @staticmethod
    def create_bar(
        percentage: float,
        length: int = 10,
        filled_char: str = "▓",
        empty_char: str = "░",
    ) -> str:
        """Create a progress bar."""
        filled = int((percentage / 100) * length)
        empty = length - filled
        return filled_char * filled + empty_char * empty

    @staticmethod
    def create_spinner(step: int) -> str:
        """Create a spinning indicator."""
        spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        return spinners[step % len(spinners)]

    @staticmethod
    def create_dots(step: int) -> str:
        """Create a dots indicator."""
        dots = ["", ".", "..", "..."]
        return dots[step % len(dots)]


class CodeHighlighter:
    """Simple code highlighting for common languages."""

    # Language file extensions mapping
    LANGUAGE_EXTENSIONS = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "javascript",
        ".tsx": "typescript",
        ".java": "java",
        ".cpp": "cpp",
        ".c": "c",
        ".cs": "csharp",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".swift": "swift",
        ".kt": "kotlin",
        ".scala": "scala",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".sql": "sql",
        ".json": "json",
        ".xml": "xml",
        ".html": "html",
        ".css": "css",
        ".scss": "scss",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
    }

    @classmethod
    def detect_language(cls, filename: str) -> str:
        """Detect programming language from filename."""
        from pathlib import Path

        ext = Path(filename).suffix.lower()
        return cls.LANGUAGE_EXTENSIONS.get(ext, "")

    @classmethod
    def format_code(cls, code: str, language: str = "", filename: str = "") -> str:
        """Format code with language detection."""
        if not language and filename:
            language = cls.detect_language(filename)

        if language:
            return f"```{language}\n{code}\n```"
        else:
            return f"```\n{code}\n```"
