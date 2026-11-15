"""Audio file validation utilities."""

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import NamedTuple, Optional

logger = logging.getLogger("streamrip")


class ValidationResult(NamedTuple):
    """Result of audio file validation."""
    is_valid: bool
    error_message: Optional[str] = None
    validation_method: Optional[str] = None


class AudioValidator:
    """Validates audio files for corruption and integrity."""

    def __init__(self):
        # Check available validation tools on initialization
        self.flac_available = shutil.which("flac") is not None
        self.ffmpeg_available = shutil.which("ffmpeg") is not None

        if not self.ffmpeg_available:
            logger.warning("ffmpeg not found - audio validation will be limited")

    async def validate_audio_file(self, file_path: str) -> ValidationResult:
        """Validate an audio file for corruption and integrity.

        Args:
            file_path: Path to the audio file to validate

        Returns:
            ValidationResult with validation status and details
        """
        if not os.path.exists(file_path):
            return ValidationResult(
                is_valid=False,
                error_message=f"File not found: {file_path}",
                validation_method="file_check"
            )

        file_extension = Path(file_path).suffix.lower()

        # For FLAC files, try flac tool first, then fallback to ffmpeg
        if file_extension == ".flac":
            if self.flac_available:
                result = await self._validate_with_flac_tool(file_path)
                if result.is_valid or result.validation_method == "flac_tool":
                    return result
                # If flac tool failed to run, fallback to ffmpeg

            # Fallback to ffmpeg for FLAC files
            if self.ffmpeg_available:
                return await self._validate_with_ffmpeg(file_path)

        # For all other formats, use ffmpeg
        elif self.ffmpeg_available:
            return await self._validate_with_ffmpeg(file_path)

        # No validation tools available
        logger.warning(f"No validation tools available for {file_path}")
        return ValidationResult(
            is_valid=False,
            error_message="No validation tools available",
            validation_method="none"
        )

    async def _validate_with_flac_tool(self, file_path: str) -> ValidationResult:
        """Validate FLAC file using the flac command-line tool.

        Args:
            file_path: Path to the FLAC file

        Returns:
            ValidationResult with validation status
        """
        try:
            logger.debug(f"Validating FLAC file with flac tool: {file_path}")

            # flac -t performs a test decode without output
            process = await asyncio.create_subprocess_exec(
                "flac", "-t", file_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            if process.returncode == 0:
                logger.debug(f"FLAC validation passed: {file_path}")
                return ValidationResult(
                    is_valid=True,
                    validation_method="flac_tool"
                )
            else:
                error_msg = stderr.decode('utf-8').strip() if stderr else "Unknown FLAC validation error"
                logger.error(f"FLAC validation failed for {file_path}: {error_msg}")
                return ValidationResult(
                    is_valid=False,
                    error_message=f"FLAC validation failed: {error_msg}",
                    validation_method="flac_tool"
                )

        except Exception as e:
            logger.debug(f"Error running flac tool for {file_path}: {e}")
            return ValidationResult(
                is_valid=False,
                error_message=f"Failed to run flac tool: {str(e)}",
                validation_method="flac_tool_error"
            )

    async def _validate_with_ffmpeg(self, file_path: str) -> ValidationResult:
        """Validate audio file using ffmpeg.

        Args:
            file_path: Path to the audio file

        Returns:
            ValidationResult with validation status
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-v", "error", "-xerror", "-i", file_path,
                "-map", "0:a:0", "-f", "null", "-",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await process.communicate()
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                raise
            error = stderr.decode("utf-8", errors="replace").strip()
            return ValidationResult(
                process.returncode == 0 and not error,
                error or None, "ffmpeg",
            )
        except OSError as e:
            return ValidationResult(False, str(e), "ffmpeg_error")


# Global validator instance
_validator = None

def get_audio_validator() -> AudioValidator:
    """Get the global audio validator instance."""
    global _validator
    if _validator is None:
        _validator = AudioValidator()
    return _validator


async def validate_audio_file(file_path: str) -> ValidationResult:
    """Convenience function to validate an audio file.

    Args:
        file_path: Path to the audio file to validate

    Returns:
        ValidationResult with validation status and details
    """
    validator = get_audio_validator()
    return await validator.validate_audio_file(file_path)