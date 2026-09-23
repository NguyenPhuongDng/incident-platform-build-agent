"""Application configuration, loaded from .env."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[2]
load_dotenv(BASE_DIR / ".env")


def _abs(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else (BASE_DIR / path).resolve()


class Settings:
    """Plain settings object; every value comes from the environment."""

    def __init__(self) -> None:
        # LLM
        self.qwen_api_key: str = os.getenv("QWEN_API_KEY", "")
        self.qwen_base_url: str = os.getenv(
            "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        )
        self.chat_model: str = os.getenv("QWEN_CHAT_MODEL", "qwen-plus")
        self.router_model: str = os.getenv("QWEN_ROUTER_MODEL", "qwen-plus")
        # Một biến mỗi agent quản trị — không tách nhỏ theo từng việc con (Evaluator
        # dùng cùng một model cho cả sinh case lẫn chấm điểm judge).
        self.builder_model: str = os.getenv("QWEN_BUILDER_MODEL") or self.chat_model
        self.evaluator_model: str = os.getenv("QWEN_EVALUATOR_MODEL") or self.chat_model
        self._evaluator_model_explicit: bool = bool(os.getenv("QWEN_EVALUATOR_MODEL"))
        self.embed_model: str = os.getenv("QWEN_EMBED_MODEL", "text-embedding-v3")
        self.embed_dim: int = int(os.getenv("EMBED_DIM", "1024"))
        self.llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "90"))
        self.llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "3"))
        self.trace_llm: bool = os.getenv("TRACE_LLM", "1").lower() not in ("0", "false", "no")

        # App
        self.app_host: str = os.getenv("APP_HOST", "127.0.0.1")
        self.app_port: int = int(os.getenv("APP_PORT", "8000"))
        self.domain_id: str = os.getenv("DOMAIN_ID", "vinhomes")
        self.db_path: Path = _abs(os.getenv("DB_PATH", "./data/demo.db"))
        self.chroma_path: Path = _abs(os.getenv("CHROMA_PATH", "./data/chroma"))

        # MCP
        self.mcp_ky_thuat_url: str = os.getenv("MCP_KY_THUAT_URL", "http://127.0.0.1:8101/mcp")
        self.mcp_port: int = int(os.getenv("MCP_PORT", "8101"))
        self.mcp_an_ninh_url: str = os.getenv("MCP_AN_NINH_URL", "http://127.0.0.1:8102/mcp")
        self.mcp_an_ninh_port: int = int(os.getenv("MCP_AN_NINH_PORT", "8102"))
        self.mcp_ve_sinh_url: str = os.getenv("MCP_VE_SINH_URL", "http://127.0.0.1:8103/mcp")
        self.mcp_ve_sinh_port: int = int(os.getenv("MCP_VE_SINH_PORT", "8103"))

        # RAG
        self.chunk_size: int = int(os.getenv("CHUNK_SIZE", "800"))
        self.chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "100"))
        self.top_k: int = int(os.getenv("TOP_K", "4"))
        self.rag_max_distance: float = float(os.getenv("RAG_MAX_DISTANCE", "1.2"))

        # Room
        self.max_tool_iterations: int = int(os.getenv("MAX_TOOL_ITERATIONS", "4"))
        self.max_agent_tools: int = int(os.getenv("MAX_AGENT_TOOLS", "10"))
        # "maf" = Microsoft Agent Framework GroupChat, "builtin" = vòng lặp tự viết
        self.room_engine: str = os.getenv("ROOM_ENGINE", "maf").lower()
        # HITL: phòng họp dừng ngay tại lượt gọi tool cần duyệt cho tới khi quản lý
        # quyết định. Tắt (=0) thì quay lại hành vi cũ: tạo hành động chờ duyệt rồi
        # chạy tiếp, agent tự kết luận khi chưa có kết quả tool.
        self.hitl_wait_for_approval: bool = os.getenv("HITL_WAIT_FOR_APPROVAL", "1").lower() not in ("0", "false", "no")
        # Hết hạn chờ thì phòng họp chạy tiếp thay vì treo vĩnh viễn (demo không có
        # ai trực 24/7). Hành động vẫn nằm ở hàng chờ, duyệt muộn vẫn thực thi.
        self.hitl_approval_timeout: float = float(os.getenv("HITL_APPROVAL_TIMEOUT", "600"))
        # Trần thời gian endpoint duyệt đợi phòng họp chạy xong tool rồi trả kết quả.
        self.hitl_execute_timeout: float = float(os.getenv("HITL_EXECUTE_TIMEOUT", "120"))
        # Số lượt nhắn của người báo trước khi Lễ tân buộc phải mở phản ánh với thông
        # tin đã có, thay vì hỏi tiếp. Model nhỏ có thể hỏi vòng vo vô hạn.
        self.intake_max_turns: int = int(os.getenv("INTAKE_MAX_TURNS", "3"))
        # Số lần TỐI ĐA phòng họp được quay lại hỏi người báo trên cùng một phản ánh.
        # Quá trần thì guard chặn, agent phải kết luận với thông tin đang có — chặn
        # vòng "hỏi -> trả lời -> hỏi lại y hệt" đã đo được trên vé thật.
        self.max_followup_rounds: int = int(os.getenv("MAX_FOLLOWUP_ROUNDS", "2"))

        # Builder / Evaluator (Phase 8-9)
        self.builder_overlap_threshold: float = float(os.getenv("BUILDER_OVERLAP_THRESHOLD", "0.85"))
        self.builder_max_iterations: int = int(os.getenv("BUILDER_MAX_ITERATIONS", "2"))
        self.eval_cases_per_agent: int = int(os.getenv("EVAL_CASES_PER_AGENT", "6"))
        self.eval_concurrency: int = int(os.getenv("EVAL_CONCURRENCY", "2"))
        self.eval_judge_min: int = int(os.getenv("EVAL_JUDGE_MIN", "3"))
        self.eval_router_repeats: int = int(os.getenv("EVAL_ROUTER_REPEATS", "2"))
        self.eval_regression_max_drop: int = int(os.getenv("EVAL_REGRESSION_MAX_DROP", "0"))
        # regression() quét TOÀN BỘ case đã duyệt của MỌI agent active, mỗi case tốn
        # eval_router_repeats × 2 tập thành viên lệnh gọi LLM. Không giới hạn, chi phí
        # này lớn dần không giới hạn theo thời gian khi bộ case hồi quy tích lũy (đo
        # thật: builder/auto với 15 case gốc từng vượt 30 phút, timeout không xong).
        # Giới hạn số case LẤY MẪU mỗi agent để chi phí luôn có trần, bất kể domain
        # đã tích lũy bao nhiêu case qua thời gian.
        self.eval_regression_max_cases_per_agent: int = int(os.getenv("EVAL_REGRESSION_MAX_CASES_PER_AGENT", "3"))

        # Paths
        self.base_dir: Path = BASE_DIR
        self.domains_dir: Path = BASE_DIR / "domains"
        self.frontend_dir: Path = BASE_DIR / "frontend"
        self.data_dir: Path = BASE_DIR / "data"

    @property
    def domain_dir(self) -> Path:
        return self.domains_dir / self.domain_id


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    return s


settings = get_settings()
