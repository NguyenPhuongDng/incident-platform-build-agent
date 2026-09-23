"""Builder: turns a manager's plain-language request into a specialist-agent draft.

Builder is an admin-tier service, not a specialist agent. It never joins the
meeting room, is never a row in the `Agent` table, and the Điều phối never sees
it. It is reachable only through `/api/builder/*`.

What it sees: the domain pack, tool *metadata* (name, description, scope, approval
flag — never a schema, never code), and other agents' `id` / `display_name` /
`capability` (never their `business_prompt`). It never sees the knowledge library
and never proposes or attaches documents — that is the manager's call alone
(principle 3b). Its output always lands as `status="draft"`; nothing here can
activate an agent.
"""
from __future__ import annotations

import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from agent_framework import Agent as MafAgent
from agent_framework.openai import OpenAIChatCompletionClient
from pydantic import ValidationError

from backend.agents import registry
from backend.agents.registry import AgentCreate, AgentUpdate, SLUG_RE
from backend.app.config import settings
from backend.app.events import bus
from backend.domain.loader import load_domain
from backend.knowledge import library
from backend.llm import qwen_client as llm
from backend.tools.catalog import get_catalog

logger = logging.getLogger("builder")

# Business-prompt instructions that would fight the platform's fixed room rules.
# Kept here (not delegated to the LLM's judgment) because a violation here is a
# platform-safety concern, not a style choice — see platform_prompt.py rule 1 & 4.
_FORBIDDEN_PROMPT_PATTERNS = [
    (re.compile(r"trả lời (trực tiếp|thẳng)", re.IGNORECASE), "cho phép agent trả lời trực tiếp người dùng"),
    (re.compile(r"tự (quyết định|phê duyệt|duyệt)", re.IGNORECASE), "cho phép agent tự quyết/tự duyệt việc cần duyệt"),
    (re.compile(r"không cần (quản lý )?duyệt", re.IGNORECASE), "cho phép bỏ qua bước duyệt"),
    (re.compile(r"bỏ qua luật phòng họp", re.IGNORECASE), "yêu cầu bỏ qua luật phòng họp"),
]

BUILDER_SYSTEM_TEMPLATE = """Bạn là trợ lý thiết kế agent chuyên môn cho platform phòng họp xử lý phản ánh của {org_name}.
Người dùng là quản lý nghiệp vụ, không phải lập trình viên. Từ yêu cầu của họ, soạn MỘT bản nháp agent.

Nguyên tắc:
1. "capability" là thứ Điều phối đọc để quyết định có gọi agent này hay không. Viết 1–3 câu, liệt kê cụ thể các loại việc agent xử lý; cố gắng nêu rõ ranh giới (việc gì KHÔNG thuộc agent này). Không viết chung chung kiểu "xử lý mọi vấn đề".
2. "business_prompt" LUÔN LUÔN là một CHUỖI VĂN BẢN markdown (không phải object/dict), đúng 3 mục theo
   mẫu sau (giữ nguyên tiêu đề "## Vai trò", "## Nhiệm vụ", "## Quy tắc riêng"):
   "## Vai trò\n<1-2 câu>\n\n## Nhiệm vụ\n1. ...\n2. ...\n\n## Quy tắc riêng\n- ...\n- ..."
   KHÔNG lặp lại luật phòng họp (hệ thống tự bọc). KHÔNG cho agent nói chuyện trực tiếp với {audience},
   không cho agent tự quyết các việc cần duyệt.
3. Chỉ chọn tool có trong catalog dưới đây, và CHỈ chọn khi mô tả của tool thực sự khớp việc cần làm.
   TUYỆT ĐỐI không mượn tạm một tool của việc khác chỉ vì không có tool đúng — ví dụ không dùng tool
   dọn vệ sinh hay báo giá sửa chữa cho việc chăm cây nếu catalog không có tool cây xanh thật sự.
   Việc cần làm mà không có tool phù hợp thì PHẢI ghi vào "thieu_tool", để trống tools cho phần đó
   còn hơn là chọn nhầm tool.
4. KHÔNG đề xuất, gắn hay nhắc tới tài liệu tri thức: việc đó do quản lý tự làm.
5. So sánh với các agent đang có (liệt kê dưới đây). Nếu năng lực trùng, ghi vào "chong_lan" và thu hẹp "capability" để tránh trùng.
6. Nếu yêu cầu bao phủ nhiều việc thuộc các bộ phận khác nhau, ghi vào "canh_bao" gợi ý tách thành nhiều agent.
Chỉ trả về JSON đúng schema (business_prompt là STRING, không phải object):
{{"id": str, "display_name": str, "capability": str, "business_prompt": "## Vai trò\n...\n\n## Nhiệm vụ\n...\n\n## Quy tắc riêng\n...", "tools": [str], "giai_thich": {{"tools": {{}}}}, "thieu_tool": [str], "chong_lan": [{{"agent_id": str, "muc_do": "cao|trung_binh|thap", "diem_trung": str}}], "canh_bao": [str]}}

CATALOG TOOL:
{catalog_block}

AGENT HIỆN CÓ:
{agents_block}

YÊU CẦU CỦA QUẢN LÝ:
{yeu_cau}"""

REVISE_SYSTEM_TEMPLATE = """Bạn là trợ lý thiết kế agent chuyên môn cho platform phòng họp xử lý phản ánh của {org_name}.
Một bản nháp agent bạn (hoặc đồng nghiệp) từng soạn đã được chạy thử và đánh giá. Hãy SỬA LẠI bản nháp
dựa trên báo cáo đánh giá bên dưới. Chỉ sửa đúng chỗ gây lỗi (mô tả năng lực, prompt nghiệp vụ, hoặc tool)
— không viết lại từ đầu, không đổi những phần đang đúng.

Nguyên tắc (giữ nguyên như lúc soạn nháp):
1. "capability" cụ thể, nêu rõ ranh giới, không chung chung.
2. "business_prompt" đúng 3 mục Vai trò / Nhiệm vụ / Quy tắc riêng, không cho agent nói chuyện trực tiếp
   với {audience}, không cho tự quyết việc cần duyệt.
3. Chỉ chọn tool có trong catalog. Việc thiếu tool ghi vào "thieu_tool".
4. KHÔNG đề xuất hay gắn tài liệu tri thức. Nếu báo cáo đánh giá cho thấy vấn đề là THIẾU TÀI LIỆU
   (ví dụ điểm "bam_nguon" thấp vì không có gì để trích dẫn), đừng tự ý xử lý — chỉ ghi rõ vào "canh_bao"
   để quản lý tự quyết định có nên upload/gắn tài liệu hay không.
5. Liệt kê CHÍNH XÁC từng thay đổi đã thực hiện vào "thay_doi", mỗi mục là một câu ngắn giải thích sửa gì và vì sao.

Chỉ trả về JSON đúng schema:
{{"id": str, "display_name": str, "capability": str, "business_prompt": str, "tools": [str], "giai_thich": {{"tools": {{}}}}, "thieu_tool": [str], "chong_lan": [...], "canh_bao": [str], "thay_doi": [str]}}

BẢN NHÁP HIỆN TẠI:
{draft_block}

BÁO CÁO ĐÁNH GIÁ:
{report_block}"""


@dataclass
class BuilderOutput:
    draft: dict[str, Any]
    giai_thich: dict[str, Any] = field(default_factory=dict)
    thieu_tool: list[str] = field(default_factory=list)
    chong_lan: list[dict[str, Any]] = field(default_factory=list)
    canh_bao: list[str] = field(default_factory=list)
    thay_doi: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "draft": self.draft,
            "giai_thich": self.giai_thich,
            "thieu_tool": self.thieu_tool,
            "chong_lan": self.chong_lan,
            "canh_bao": self.canh_bao,
        }
        if self.thay_doi:
            out["thay_doi"] = self.thay_doi
        return out


def _slugify(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9_-]+", "_", s).strip("_-")
    s = re.sub(r"_{2,}", "_", s)
    return s[:41] or "agent"


def _unique_id(candidate: str, taken: set[str]) -> str:
    base = candidate if SLUG_RE.match(candidate) else _slugify(candidate)
    if not SLUG_RE.match(base):
        base = f"agent_{abs(hash(candidate)) % 10_000}"
    if base not in taken:
        return base
    for i in range(2, 100):
        alt = f"{base}_{i}"[:41]
        if alt not in taken:
            return alt
    return f"{base}_{abs(hash(candidate)) % 10_000}"


def _catalog_block() -> str:
    lines = []
    for spec in get_catalog().all():
        flag = " [cần duyệt]" if spec.requires_approval else ""
        lines.append(f"- {spec.name} ({spec.scope}){flag}: {spec.manager_description}")
    return "\n".join(lines) or "(chưa có tool nào)"


def _agents_block(domain_id: str) -> str:
    agents = [a for a in registry.list_agents(domain_id) if a["status"] in ("active", "draft")]
    if not agents:
        return "(chưa có agent chuyên môn nào)"
    return "\n".join(f"- {a['id']} | {a['display_name']}: {a['capability']}" for a in agents)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _check_overlap(domain_id: str, capability: str, exclude_id: str | None) -> list[dict[str, Any]]:
    """Code-side overlap check via embeddings — catches cases the LLM's own
    self-report misses (it compares text, not meaning, and it graded its own work)."""
    agents = [
        a for a in registry.list_agents(domain_id)
        if a["status"] in ("active", "draft") and a["id"] != exclude_id
    ]
    if not agents:
        return []
    try:
        vectors = llm.embed([capability] + [a["capability"] for a in agents], role="builder_overlap")
    except llm.LLMError as exc:
        logger.warning("Không kiểm tra được chồng lấn bằng embedding: %s", exc)
        return []
    new_vec, others = vectors[0], vectors[1:]
    out = []
    for agent, vec in zip(agents, others):
        sim = _cosine(new_vec, vec)
        if sim >= settings.builder_overlap_threshold:
            level = "cao" if sim >= 0.93 else "trung_binh"
            out.append({
                "agent_id": agent["id"],
                "muc_do": level,
                "diem_trung": f"độ tương đồng embedding {sim:.2f} (ngưỡng {settings.builder_overlap_threshold})",
            })
    return out


_NEGATION_WORDS = ("không", "cấm", "chưa", "đừng", "tuyệt đối không")
_NEGATION_WINDOW = 20  # ký tự nhìn ngược trước cụm khớp, đủ cho "Không được", "tuyệt đối không"...


def _is_negated(line: str, match_start: int) -> bool:
    """A bullet like "Không tự quyết định phí" is a SAFETY instruction, not a
    violation — the forbidden phrase only matters when nothing negates it."""
    window = line[max(0, match_start - _NEGATION_WINDOW): match_start].lower()
    return any(neg in window for neg in _NEGATION_WORDS)


def _sanitize_business_prompt(text: str) -> tuple[str, list[str]]:
    """Strip lines that would fight the fixed room rules; report what got cut."""
    warnings: list[str] = []
    kept_lines = []
    for line in text.splitlines():
        hit = None
        for pat, msg in _FORBIDDEN_PROMPT_PATTERNS:
            m = pat.search(line)
            if m and not _is_negated(line, m.start()):
                hit = msg
                break
        if hit:
            warnings.append(f'Đã bỏ một dòng trong prompt nghiệp vụ vì "{hit}": "{line.strip()[:80]}"')
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines), warnings


def _coerce_business_prompt(value: Any) -> tuple[str, list[str]]:
    """The schema asks for a markdown string; small models sometimes answer with a
    nested object instead. Rebuild the expected 3-section markdown from it rather
    than falling back to a raw Python-repr dump, and flag it so it's visible."""
    if isinstance(value, str):
        return value, []
    if isinstance(value, dict):
        key_map = {
            "vai_tro": "Vai trò", "role": "Vai trò",
            "nhiem_vu": "Nhiệm vụ", "nhiemvu": "Nhiệm vụ", "task": "Nhiệm vụ", "tasks": "Nhiệm vụ",
            "quy_tac_rieng": "Quy tắc riêng", "quytacrieng": "Quy tắc riêng", "rules": "Quy tắc riêng",
        }
        sections = []
        for k, v in value.items():
            heading = key_map.get(str(k).strip().lower().replace(" ", "_"), str(k))
            body = "\n".join(f"- {x}" for x in v) if isinstance(v, list) else str(v)
            sections.append(f"## {heading}\n{body}")
        return "\n\n".join(sections), ["Builder trả về prompt nghiệp vụ dạng object thay vì văn bản — đã tự chuyển sang markdown, nên xem lại"]
    return str(value or ""), []


def _tool_relevance_check(capability: str, tools: list[str]) -> list[str]:
    """Code-side sanity check on top of the prompt instruction: an agent borrowing
    a clearly unrelated tool (bài học từ vụ Kế toán/giảm phí) should be flagged even
    if the model didn't self-report it in canh_bao."""
    if not tools or not capability:
        return []
    catalog = get_catalog()
    descriptions = [catalog.get(t).manager_description for t in tools if catalog.get(t)]
    if not descriptions:
        return []
    try:
        vectors = llm.embed([capability] + descriptions, role="builder_tool_relevance")
    except llm.LLMError:
        return []
    cap_vec, tool_vecs = vectors[0], vectors[1:]
    warnings = []
    for name, vec in zip([t for t in tools if catalog.get(t)], tool_vecs):
        if _cosine(cap_vec, vec) < 0.30:
            warnings.append(f"Tool '{name}' có vẻ không liên quan tới mô tả năng lực — kiểm tra lại trước khi lưu")
    return warnings


def _merge_overlap(llm_reported: list[Any], code_found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_agent: dict[str, dict[str, Any]] = {}
    rank = {"thap": 0, "trung_binh": 1, "cao": 2}
    for item in llm_reported or []:
        if isinstance(item, dict) and item.get("agent_id"):
            by_agent[item["agent_id"]] = {
                "agent_id": item["agent_id"],
                "muc_do": item.get("muc_do", "thap"),
                "diem_trung": item.get("diem_trung", ""),
            }
    for item in code_found:
        existing = by_agent.get(item["agent_id"])
        if existing is None or rank.get(item["muc_do"], 0) > rank.get(existing["muc_do"], 0):
            by_agent[item["agent_id"]] = item
    return list(by_agent.values())


def _validate_and_clean(raw: dict[str, Any], domain_id: str, *, exclude_id: str | None = None) -> BuilderOutput:
    if not isinstance(raw, dict):
        raw = {}

    catalog = get_catalog()
    canh_bao: list[str] = list(raw.get("canh_bao") or [])

    # Builder must never propose documents; if the model does anyway, drop and log.
    for leaked_key in ("docs", "tai_lieu", "knowledge", "documents"):
        if leaked_key in raw:
            logger.warning("Builder trả về trường '%s' dù bị cấm — đã bỏ qua", leaked_key)
            raw.pop(leaked_key, None)

    taken_ids = {a["id"] for a in registry.list_agents(domain_id)} - ({exclude_id} if exclude_id else set())
    raw_id = str(raw.get("id") or raw.get("display_name") or "agent").strip()
    agent_id = exclude_id or _unique_id(raw_id, taken_ids)

    display_name = str(raw.get("display_name") or agent_id.replace("_", " ").title()).strip()
    capability = str(raw.get("capability") or "").strip()

    tools_in = [str(t) for t in (raw.get("tools") or [])]
    valid_tools = [t for t in tools_in if catalog.get(t) is not None]
    dropped_tools = [t for t in tools_in if t not in valid_tools]
    if dropped_tools:
        canh_bao.append(f"Đã bỏ tool không có trong catalog: {', '.join(dropped_tools)}")
    if len(valid_tools) > settings.max_agent_tools:
        canh_bao.append(f"Đã cắt bớt còn {settings.max_agent_tools} tool (giới hạn tối đa)")
        valid_tools = valid_tools[: settings.max_agent_tools]

    business_prompt_raw, coerce_warnings = _coerce_business_prompt(raw.get("business_prompt"))
    business_prompt, prompt_warnings = _sanitize_business_prompt(business_prompt_raw)
    canh_bao.extend(coerce_warnings)
    canh_bao.extend(prompt_warnings)
    canh_bao.extend(_tool_relevance_check(capability, valid_tools))

    code_overlap = _check_overlap(domain_id, capability, agent_id) if capability else []
    chong_lan = _merge_overlap(raw.get("chong_lan"), code_overlap)

    draft = {
        "id": agent_id,
        "display_name": display_name,
        "capability": capability,
        "business_prompt": business_prompt,
        "tools": valid_tools,
        "status": "draft",
        "domain_id": domain_id,
    }

    # Principle 2: the draft must pass the same schema `POST /api/agents` uses.
    # A failure here (e.g. capability still under 20 chars) does not crash the
    # request — it's surfaced as a warning so the manager fixes it in the form,
    # which already guides them (character counter, slug pattern) before Save.
    try:
        AgentCreate(**{k: v for k, v in draft.items() if k != "domain_id"})
    except ValidationError as exc:
        for err in exc.errors()[:5]:
            canh_bao.append(f"Bản nháp chưa hợp lệ ({'.'.join(str(x) for x in err['loc'])}): {err['msg']}")

    giai_thich = raw.get("giai_thich") if isinstance(raw.get("giai_thich"), dict) else {}
    thieu_tool = [str(x) for x in (raw.get("thieu_tool") or [])]
    thay_doi = [str(x) for x in (raw.get("thay_doi") or [])]

    return BuilderOutput(
        draft=draft, giai_thich=giai_thich, thieu_tool=thieu_tool,
        chong_lan=chong_lan, canh_bao=canh_bao, thay_doi=thay_doi,
    )


def _chat_client() -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=settings.builder_model, api_key=settings.qwen_api_key, base_url=settings.qwen_base_url,
    )


def _run_json(system_prompt: str, *, role: str) -> dict[str, Any]:
    """MAF `Agent.run` in JSON mode; falls back to the battle-tested `chat_json`
    helper if MAF's output can't be parsed even after one retry, so a framework
    hiccup never blocks the Builder."""
    try:
        import anyio

        async def _once() -> str:
            agent = MafAgent(_chat_client(), "Bạn chỉ trả lời bằng JSON.", name=role)
            r = await agent.run(system_prompt, options={"response_format": {"type": "json_object"}})
            return r.text or ""

        text = anyio.run(_once)
        return llm.extract_json(text)
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the request
        logger.warning("MAF Builder lỗi (%s), chuyển sang chat_json trực tiếp", str(exc)[:200])
        return llm.chat_json(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": "Soạn bản nháp."}],
            role=role, model=settings.builder_model,
        )


def draft(domain_id: str, yeu_cau: str, agent_id_goi_y: str | None = None) -> BuilderOutput:
    domain = load_domain(domain_id)
    system = BUILDER_SYSTEM_TEMPLATE.format(
        org_name=domain.display_name,
        audience=domain.audience,
        catalog_block=_catalog_block(),
        agents_block=_agents_block(domain_id),
        yeu_cau=yeu_cau.strip(),
    )
    raw = _run_json(system, role="builder_draft")
    if agent_id_goi_y and SLUG_RE.match(agent_id_goi_y):
        raw = {**raw, "id": agent_id_goi_y}
    return _validate_and_clean(raw, domain_id)


def revise(domain_id: str, current_draft: dict[str, Any], bao_cao_danh_gia: dict[str, Any]) -> BuilderOutput:
    import json

    domain = load_domain(domain_id)
    existing_id = current_draft.get("id")
    system = REVISE_SYSTEM_TEMPLATE.format(
        org_name=domain.display_name,
        audience=domain.audience,
        draft_block=json.dumps(current_draft, ensure_ascii=False, indent=1),
        report_block=json.dumps(bao_cao_danh_gia, ensure_ascii=False, indent=1),
    )
    raw = _run_json(system, role="builder_revise")
    if existing_id:
        raw = {**raw, "id": existing_id}
    return _validate_and_clean(raw, domain_id, exclude_id=existing_id)


def auto_loop(domain_id: str, yeu_cau: str, doc_ids: list[str] | None = None,
              *, run_token: str | None = None) -> dict[str, Any]:
    """Builder soạn nháp → Evaluator sinh case và chấm → nếu trượt, Builder tự sửa
    và chạy lại, tối đa `BUILDER_MAX_ITERATIONS` vòng. Không bao giờ tự bật agent —
    quản lý luôn là người bấm Bật cuối cùng. `doc_ids` là tài liệu quản lý đã tự
    chọn/upload trước khi bấm nút; giữ nguyên suốt các vòng, Builder không đụng vào.
    """
    from backend.agents import evaluator  # local import: evaluator does not import builder back

    token = run_token or f"AUTO-{uuid.uuid4().hex[:8].upper()}"

    def emit(type_: str, payload: dict[str, Any]) -> None:
        bus.emit(f"BUILDERAUTO-{token}", type_, payload)

    out = draft(domain_id, yeu_cau)
    emit("builder_draft", out.to_dict())

    created = registry.create_agent(
        AgentCreate(id=out.draft["id"], display_name=out.draft["display_name"],
                   capability=out.draft["capability"], business_prompt=out.draft["business_prompt"],
                   tools=out.draft["tools"], status="draft", domain_id=domain_id)
    )
    agent_id = created["id"]
    for doc_id in doc_ids or []:
        try:
            library.link(agent_id, doc_id)
        except Exception as exc:  # noqa: BLE001 - a bad doc_id must not abort the loop
            logger.warning("Không gắn được tài liệu %s cho %s: %s", doc_id, agent_id, exc)
    emit("agent_created", {"agent_id": agent_id, "doc_ids": doc_ids or []})

    cases = evaluator.generate_cases(agent_id, auto_approve=True)
    emit("cases_generated", {"so_case": len(cases)})

    history: list[dict[str, Any]] = []
    passed = False
    run_result: dict[str, Any] = {}
    reg: dict[str, Any] = {}
    for round_no in range(1, settings.builder_max_iterations + 1):
        emit("round_start", {"round": round_no})
        run_result = evaluator.run_agent(agent_id, trigger="builder_loop")
        reg = evaluator.regression(candidate_agent_id=agent_id, domain_id=domain_id)
        passed = run_result["passed"] and reg["dat"]
        history.append({
            "round": round_no, "run_id": run_result["run_id"], "passed": run_result["passed"],
            "regression_dat": reg["dat"], "case_fail": run_result["summary"]["case_fail"],
        })
        emit("round_result", history[-1])
        if passed or round_no == settings.builder_max_iterations:
            break

        report = {"eval": run_result["summary"], "results": run_result["results"], "regression": reg}
        revised = revise(domain_id, {**out.draft, "id": agent_id}, report)
        emit("builder_revise", revised.to_dict())
        registry.update_agent(agent_id, AgentUpdate(
            capability=revised.draft["capability"], business_prompt=revised.draft["business_prompt"],
            tools=revised.draft["tools"],
        ))
        out = revised

    final_agent = registry.get_agent(agent_id, with_docs=True)
    result = {
        "agent_id": agent_id, "passed": passed, "history": history,
        "last_run": run_result, "last_regression": reg, "agent": final_agent,
    }
    emit("auto_done", {"agent_id": agent_id, "passed": passed, "so_vong": len(history)})
    return result
