import asyncio
import re
from src.tools.common import tool, ToolContext
from src.core.storage import get_session_dir


def _report_filename(title: str) -> str:
    stem = title.replace(" ", "_").lower()
    # drop path separators and other characters that aren't filename-safe
    stem = re.sub(r"[^a-z0-9_\-]", "", stem) or "report"
    return f"{stem}.md"


@tool(name="write_report", description="Write a structured markdown report to disk")
async def write_report(title: str, sections: str, _ctx: ToolContext = None) -> str:
    """
    Persist a markdown report under the session's reports directory.

    Args:
        title: report title, also used to derive the filename
        sections: full markdown content of the report body
    """
    reports_dir = get_session_dir(_ctx.working_dir, _ctx.storage_session_id) / "reports"
    report_path = reports_dir / _report_filename(title)

    def _write():
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path.write_text(f"# {title}\n\n{sections}\n", encoding="utf-8")

    await asyncio.to_thread(_write)
    return str(report_path.resolve())
