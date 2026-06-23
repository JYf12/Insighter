"""
Markdown 文件生成工具

供主智能体把最终整理后的内容写入当前会话工作目录。工具会把模型传入的
filename/path 交给 resolve_path 统一解析，避免模型直接操作真实绝对路径。

注意：计时/埋点/指标更新已由 observability_middleware 统一接管，
工具文件不再需要手工调用 time.perf_counter() 或 monitor.report_tool_end/failure()。
"""

from pathlib import Path

try:
    from typing import Annotated
except ImportError:
    from typing_extensions import Annotated
from langchain_core.tools import tool

from app.api.context import get_session_context
from app.api.monitor import monitor
from app.utils.logger import get_logger
from app.utils.path_utils import resolve_path

_logger = get_logger("markdown_tool")


@tool
def generate_markdown(
    content: Annotated[str, "要写入Markdown文档的文本内容"],
    filename: Annotated[str, "Markdown文档的文件名（不包含扩展名或包含.md）"],
    path: Annotated[str, "文件保存的绝对路径"] = "",
):
    """
    根据提供的文本内容生成 Markdown 文件

    :param content: 要写入 Markdown 文档的完整文本
    :param filename: 输出文件名，缺少 .md 后缀时会自动补全
    :param path: 可选保存路径；通常由运行时工作目录指令约束为相对路径
    :return: 文件生成结果说明
    """
    _logger.debug("输入保存路径", extra={"path": path or "当前会话目录"})
    monitor.report_tool("Markdown文档生成工具", {"写入的文本内容": content[:max(0, 200)]})
    if not filename.endswith(".md"):
        filename += ".md"

    # session_dir 由 run_deep_agent 写入 ContextVar，保证文件写入当前会话工作目录
    session_dir = get_session_context()
    _logger.debug("当前会话目录", extra={"session_dir": session_dir})

    # 先把模型传入的 path/filename 合成一个逻辑路径，再交给 resolve_path 做统一清洗
    if path and path != ".":
        full_input_path = str(Path(path) / filename)
    else:
        full_input_path = filename
    full_path_str = resolve_path(full_input_path, session_dir)
    file_path = Path(full_path_str)

    parent_dir = file_path.parent

    _logger.debug("Markdown 路径解析", extra={
        "parent_dir": str(parent_dir), "filename": filename, "full_path": str(file_path),
    })

    # 允许模型指定 session_dir 下的子目录；不存在时自动创建
    if not parent_dir.exists():
        parent_dir.mkdir(parents=True, exist_ok=True)
        _logger.debug("已创建目录", extra={"dir": str(parent_dir)})

    file_path.write_text(content, encoding="utf-8")

    _logger.info("文件写入完成", extra={"file_path": str(file_path)})
    return f"Markdown文件 '{file_path}' 已成功生成并保存。"


if __name__ == "__main__":
    # 本地调试入口：直接运行本文件可验证 Markdown 写入和路径解析效果
    def get_session_context():
        return "./examples/test_docs"

    test_content = "# 测试文档\n这是 Markdown 生成工具的本地测试内容"
    test_filename = "测试文件"
    test_path = "sub_dir"

    print("===== 开始测试：Markdown 文件生成 =====")
    result = generate_markdown.invoke(
        {"content": test_content, "filename": test_filename, "path": test_path}
    )

    print(f"\n调用结果：{result}")
    if "已成功生成" in result:
        file_path = Path(result.split("'")[1])
        print(f"验证结果：文件 {file_path} {'存在' if file_path.exists() else '不存在'}")
