"""Script to generate a comprehensive Word (.docx) technical report for FinResolve AI.
Includes:
- Detailed Database Architecture and ERD Schema (19 tables across 6 functional groups)
- System Architecture and Operational Workflow (7 layers, Sequence diagram flow, State Machine)
- Detailed Real-World Scenario: Fake Bill / Fraudulent Payment Defense Analysis
- In-depth Agentic AI Architecture & LLM Deployment Analysis (Matching the User's Agentic AI Infographic)
- Deployment and Production Go-Live Roadmap
"""

import os
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_ALIGN_VERTICAL
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

def set_cell_background(cell, fill_hex):
    shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill_hex}"/>')
    cell._tc.get_or_add_tcPr().append(shd)

def set_cell_margins(cell, top=100, bottom=100, left=150, right=150):
    tcPr = cell._tc.get_or_add_tcPr()
    tcMar = parse_xml(
        f'<w:tcMar {nsdecls("w")}>'
        f'<w:top w:w="{top}" w:type="dxa"/>'
        f'<w:bottom w:w="{bottom}" w:type="dxa"/>'
        f'<w:left w:w="{left}" w:type="dxa"/>'
        f'<w:right w:w="{right}" w:type="dxa"/>'
        f'</w:tcMar>'
    )
    tcPr.append(tcMar)

def format_table(table, header_bg="1E3A8A", alt_row_bg="F8FAFC"):
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    header_tr = table.rows[0]
    trPr = header_tr._tr.get_or_add_trPr()
    trPr.append(parse_xml(f'<w:tblHeader {nsdecls("w")}/>'))

    for cell in header_tr.cells:
        set_cell_background(cell, header_bg)
        set_cell_margins(cell, top=120, bottom=120, left=150, right=150)
        cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
        for p in cell.paragraphs:
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            for r in p.runs:
                r.font.bold = True
                r.font.color.rgb = RGBColor(255, 255, 255)
                r.font.size = Pt(10)

    for row_idx, row in enumerate(table.rows[1:], start=1):
        bg = alt_row_bg if row_idx % 2 == 1 else "FFFFFF"
        for cell in row.cells:
            set_cell_background(cell, bg)
            set_cell_margins(cell, top=80, bottom=80, left=120, right=120)
            cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            for p in cell.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(9.5)
                    r.font.color.rgb = RGBColor(30, 41, 59)

def add_callout_box(doc, text_list, title="LƯU Ý QUAN TRỌNG", border_color="1E3A8A", bg_color="EFF6FF"):
    tbl = doc.add_table(rows=1, cols=1)
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = tbl.cell(0, 0)
    set_cell_background(cell, bg_color)
    set_cell_margins(cell, top=140, bottom=140, left=200, right=200)

    tcPr = cell._tc.get_or_add_tcPr()
    tcBorders = parse_xml(
        f'<w:tcBorders {nsdecls("w")}>'
        f'<w:top w:val="none"/>'
        f'<w:left w:val="single" w:sz="36" w:space="0" w:color="{border_color}"/>'
        f'<w:bottom w:val="none"/>'
        f'<w:right w:val="none"/>'
        f'</w:tcBorders>'
    )
    tcPr.append(tcBorders)

    p0 = cell.paragraphs[0]
    p0.paragraph_format.space_before = Pt(2)
    p0.paragraph_format.space_after = Pt(4)
    r_title = p0.add_run(f"📌 {title}: ")
    r_title.bold = True
    r_title.font.size = Pt(10.5)
    r_title.font.color.rgb = RGBColor(30, 58, 138)

    for idx, t in enumerate(text_list):
        if idx == 0:
            p = p0
            r = p.add_run(t)
            r.font.size = Pt(10)
            r.font.color.rgb = RGBColor(30, 41, 59)
        else:
            p = cell.add_paragraph()
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(f"• {t}")
            r.font.size = Pt(9.5)
            r.font.color.rgb = RGBColor(30, 41, 59)
    doc.add_paragraph().paragraph_format.space_after = Pt(6)

def generate_report(output_path: str):
    doc = Document()

    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1)
        section.right_margin = Inches(1)

    style_normal = doc.styles['Normal']
    style_normal.font.name = 'Calibri'
    style_normal.font.size = Pt(11)
    style_normal.font.color.rgb = RGBColor(30, 41, 59)

    # -------------------------------------------------------------
    # COVER / HEADER TITLE
    # -------------------------------------------------------------
    title_p = doc.add_paragraph()
    title_p.paragraph_format.space_before = Pt(12)
    title_p.paragraph_format.space_after = Pt(4)
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_title = title_p.add_run("BÁO CÁO KỸ THUẬT VÀ THIẾT KẾ HỆ THỐNG")
    run_title.bold = True
    run_title.font.size = Pt(22)
    run_title.font.color.rgb = RGBColor(30, 58, 138)

    sub_p = doc.add_paragraph()
    sub_p.paragraph_format.space_after = Pt(16)
    sub_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run_sub = sub_p.add_run("FINRESOLVE AI — NỀN TẢNG AGENTIC AI ĐIỀU TRA NGOẠI LỆ & RỦI RO THANH TOÁN (v1.2 MVP)")
    run_sub.font.size = Pt(13)
    run_sub.font.bold = True
    run_sub.font.color.rgb = RGBColor(100, 116, 139)

    meta_table = doc.add_table(rows=5, cols=2)
    meta_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    metadata = [
        ("Dự án", "FinResolve AI (AI-Native Exception & Risk Resolution)"),
        ("Phiên bản", "1.2 (MVP Architecture & Production Readiness)"),
        ("Thời gian tài liệu", "22/11/2026"),
        ("Kiến trúc AI", "Agentic AI System (Bounded Planner, ModelAdapter, Tool Calling, Memory)"),
        ("Chủ đề tài liệu", "Cấu trúc CSDL (ERD), Vận hành Agentic AI Loop, Phân tích ca Bill Giả & Triển khai"),
    ]
    for i, (k, v) in enumerate(metadata):
        c0, c1 = meta_table.rows[i].cells
        c0.width = Inches(2.0)
        c1.width = Inches(4.5)
        set_cell_background(c0, "F1F5F9")
        set_cell_background(c1, "FFFFFF")
        set_cell_margins(c0, 60, 60, 100, 100)
        set_cell_margins(c1, 60, 60, 100, 100)
        p0 = c0.paragraphs[0]
        r0 = p0.add_run(k)
        r0.bold = True
        r0.font.size = Pt(10)
        r0.font.color.rgb = RGBColor(51, 65, 85)
        p1 = c1.paragraphs[0]
        r1 = p1.add_run(v)
        r1.font.size = Pt(10)
        r1.font.color.rgb = RGBColor(30, 41, 59)

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # -------------------------------------------------------------
    # SECTION 1: TỔNG QUAN
    # -------------------------------------------------------------
    h1 = doc.add_heading("1. TỔNG QUAN DỰ ÁN & BỐI CẢNH VẬN HÀNH", level=1)
    h1.paragraph_format.space_before = Pt(16)
    h1.paragraph_format.space_after = Pt(6)

    doc.add_paragraph(
        "FinResolve AI là nền tảng AI-Native Agentic AI phục vụ tự động hóa việc điều tra, phát hiện rủi ro và "
        "giải quyết các ngoại lệ phát sinh trong hoạt động thương mại điện tử và thanh toán (Payment & Commerce Exceptions). "
        "Sản phẩm được định hình dựa trên phương châm cốt lõi: 'From anomalous business events to evidence-grounded, verified resolution'."
    )

    add_callout_box(
        doc,
        [
            "LLM chỉ là công cụ hiểu ngôn ngữ và trích xuất ý định (Inference Tool), tuyệt đối KHÔNG phải là nguồn sự thật (Source of Truth).",
            "Mọi quyết định tài chính (hoàn tiền, đồng bộ đơn hàng) phải đi qua lớp Policy Engine xác định (Deterministic Code) và bảo vệ bằng chữ ký HMAC.",
            "Phân định tuyệt đối giữa CLAIM (lời khai/ảnh chưa xác thực của khách) và OBSERVED_FACT (dữ kiện đã đối soát qua sổ cái Cổng thanh toán).",
            "Verified Resolution: Không một khiếu nại nào được đóng (RESOLVED) nếu thiếu bước đọc lại trực tiếp từ CSDL chứng minh hậu điều kiện đã đạt."
        ],
        title="TRIẾT LÝ AN TOÀN PHÒNG VỆ CỐT LÕI (SAFETY INVARIANTS)"
    )

    # -------------------------------------------------------------
    # SECTION 2: KIẾN TRÚC AGENTIC AI & MÔ HÌNH LLM (THE NEW CORE SECTION)
    # -------------------------------------------------------------
    h2_ai = doc.add_heading("2. KIẾN TRÚC AGENTIC AI & CƠ CHẾ TRIỂN KHAI LLM (ĐỐI CHIẾU MÔ HÌNH)", level=1)
    h2_ai.paragraph_format.space_before = Pt(16)
    h2_ai.paragraph_format.space_after = Pt(6)

    doc.add_paragraph(
        "Để hiểu rõ bản chất của FinResolve AI, cần phân biệt rõ ràng giữa 3 mô hình AI thế hệ cũ "
        "(LLM Chatbot, RPA, Simple RAG) và mô hình Agentic AI toàn diện mà hệ thống đang triển khai:"
    )

    # Comparison Table
    ai_compare = [
        ("1. LLM Chatbot", "Truy vấn (Query) -> Trả lời văn bản (Text Response)", "Chỉ trả lời câu hỏi, KHÔNG có khả năng gọi công cụ (No Tool) và KHÔNG thể hành động (No Action)."),
        ("2. RPA (Robotic Process Automation)", "Kích hoạt (Trigger) -> Script RPA -> Thực thi hành động cố định", "Tự động hóa các tác vụ lặp đi lặp lại dạng kịch bản cứng, KHÔNG có khả năng suy luận, KHÔNG hiểu ngữ cảnh bất thường."),
        ("3. Simple RAG (Retrieval-Augmented)", "Truy vấn (Query) -> Tìm kiếm Vector DB -> Trả về tài liệu liên quan", "Chỉ tìm kiếm và cung cấp thêm thông tin, KHÔNG đưa ra quyết định, KHÔNG có vòng lặp kiểm tra và KHÔNG thực thi hành động."),
        ("4. AGENTIC AI (Hệ thống FinResolve AI)", "Goal -> Memory + Tools + LLM -> Vòng lặp Agent Loop (Plan -> Act -> Observe -> Reflect) -> Output an toàn", "Hiểu mục tiêu nghiệp vụ, tự lập kế hoạch nhiều bước, tự chọn công cụ đối soát CSDL/MoMo, quan sát kết quả, phản tư cập nhật kế hoạch và xác minh kết quả."),
    ]
    t_ai = doc.add_table(rows=len(ai_compare) + 1, cols=3)
    t_ai.rows[0].cells[0].paragraphs[0].text = "Mô hình"
    t_ai.rows[0].cells[1].paragraphs[0].text = "Cơ chế hoạt động"
    t_ai.rows[0].cells[2].paragraphs[0].text = "Đặc điểm & Giới hạn"
    t_ai.rows[0].cells[0].width = Inches(1.8)
    t_ai.rows[0].cells[1].width = Inches(2.2)
    t_ai.rows[0].cells[2].width = Inches(2.5)
    for i, (m, mech, lim) in enumerate(ai_compare, start=1):
        r = t_ai.rows[i]
        r.cells[0].paragraphs[0].text = m
        r.cells[1].paragraphs[0].text = mech
        r.cells[2].paragraphs[0].text = lim
    format_table(t_ai, header_bg="1E3A8A")

    doc.add_paragraph().paragraph_format.space_after = Pt(8)

    doc.add_heading("2.1. Năm Thành phần Cốt lõi của Agentic AI trong FinResolve AI", level=2)
    doc.add_paragraph(
        "Hệ thống FinResolve AI hiện thực hóa chuẩn chỉnh 5 thành phần cốt lõi của một hệ thống Agentic AI hiện đại "
        "(tương ứng 1:1 với sơ đồ Agentic AI Architecture):"
    )

    agent_components = [
        (
            "1. Reasoning & Planning (Suy luận & Lập kế hoạch)",
            "app/ai_core/runtime.py (BoundedAgentRuntime)",
            "Tiếp nhận Mục tiêu (Goal). Thay vì trả lời ngay, LLM suy luận chia nhỏ mục tiêu thành các giả thuyết (Hypothesis) và các bước đọc dữ liệu cần thiết (ReadToolRequest). Áp dụng giới hạn cứng (Bounded Plan: tối đa 6 bước, 2 lần lập lại kế hoạch) để triệt tiêu nguy cơ lặp vô hạn."
        ),
        (
            "2. Tool Use (Sử dụng Công cụ Bên ngoài)",
            "app/connectors/read_tools.py (ReadToolRegistry)",
            "Agent được trang bị các công cụ đọc an toàn được cấp phép: get_order (tra cứu đơn hàng), find_payment_candidates (đối soát giao dịch MoMo), get_customer_balance (kiểm tra hạn mức), check_inventory_levels (kiểm tra tồn kho). Mọi công cụ đều bị cách ly theo session_id."
        ),
        (
            "3. Memory (Hệ thống Bộ nhớ Đa tầng)",
            "app/ai_core/context.py & app/models/cases.py",
            "• Bộ nhớ ngắn hạn (Short-term Working Memory): InvestigationContext lưu giữ ngữ cảnh phiên làm việc, các bằng chứng vừa thu thập, mẩu hội thoại gần nhất và số token tiêu thụ.\n• Bộ nhớ dài hạn (Long-term Durable Memory): Bảng evidence (lưu vết CLAIM và OBSERVED_FACT) cùng bảng case_events (dòng thời gian kiểm toán bất biến trong PostgreSQL)."
        ),
        (
            "4. Knowledge / Perception (Tri giác & RAG)",
            "app/ai_core/understanding.py & context.py",
            "Bộ phân tích ngữ nghĩa tiếng Việt tự nhiên bóc tách thực thể (Mã đơn ORD-..., Mã MoMo MOMO_TXN_..., Số tiền VND 250k/250.000đ). Đồng thời tích hợp bộ lọc chống tấn công Prompt Injection, tự động vô hiệu hóa các câu lệnh độc hại cố tình chiếm quyền."
        ),
        (
            "5. The Continuous Agent Loop (Vòng lặp Agentic)",
            "app/ai_core/runtime.py (Vòng lặp Bounded Loop)",
            "Chu trình 4 bước khép kín:\n1. PLAN: Đánh giá Goal và ngữ cảnh hiện tại để chọn bước tiếp theo.\n2. ACT: Gọi công cụ đọc an toàn (Read Tools).\n3. OBSERVE: Tiếp nhận kết quả đối soát thực tế từ MoMo/OMS (OBSERVED_FACT).\n4. REFLECT: Cập nhật lại kế hoạch, kiểm tra xem đã đủ chứng cứ để kết luận hay cần điều tra tiếp."
        )
    ]
    t_agent = doc.add_table(rows=len(agent_components) + 1, cols=3)
    t_agent.rows[0].cells[0].paragraphs[0].text = "Thành phần Agentic"
    t_agent.rows[0].cells[1].paragraphs[0].text = "File mã nguồn phụ trách"
    t_agent.rows[0].cells[2].paragraphs[0].text = "Cơ chế triển khai kỹ thuật"
    t_agent.rows[0].cells[0].width = Inches(1.8)
    t_agent.rows[0].cells[1].width = Inches(2.2)
    t_agent.rows[0].cells[2].width = Inches(2.5)
    for i, (comp, file_src, desc) in enumerate(agent_components, start=1):
        r = t_agent.rows[i]
        r.cells[0].paragraphs[0].text = comp
        r.cells[1].paragraphs[0].text = file_src
        r.cells[2].paragraphs[0].text = desc
    format_table(t_agent, header_bg="047857")  # Emerald Green for Agentic Core

    doc.add_paragraph().paragraph_format.space_after = Pt(8)

    doc.add_heading("2.2. Cơ chế Triển khai LLM: ModelAdapter & Fallback DEMO_RULES", level=2)
    doc.add_paragraph(
        "Mô hình LLM trong FinResolve AI được trừu tượng hóa qua giao diện `ModelAdapter` "
        "(trong `app/ai_core/model_adapter.py`) nhằm đảm bảo tính linh hoạt và khả năng hoán đổi mà không làm thay đổi logic lõi:"
    )

    llm_modes = [
        (
            "Chế độ LLM_API (Mô hình Trí tuệ Nhân tạo thật)",
            "Kích hoạt khi cấu hình các biến môi trường: MODEL_PROVIDER (OpenAI, Gemini, Anthropic), MODEL_NAME (gemini-1.5-flash, gpt-4o), và MODEL_API_KEY. "
            "ModelAdapter giao tiếp qua giao thức HTTP bất đồng bộ (httpx), ép buộc định dạng đầu ra chuẩn qua JSON Schema (Pydantic Plan Model). "
            "Kiểm soát ngân sách cứng: MODEL_TIMEOUT_SECONDS = 20s và MODEL_BUDGET_TOKENS = 20.000 tokens."
        ),
        (
            "Chế độ DEMO_RULES (Fallback Trung thực & Xác định)",
            "Kích hoạt khi KHÔNG cấu hình API Key. Hệ thống tự động chuyển sang DeterministicDemoAdapter. "
            "Toàn bộ kết quả suy luận tuân theo bộ quy tắc nghiệp vụ xác định, mọi kết quả trên giao diện đều có tiền tố rõ ràng [DEMO_RULES] "
            "và UI ghi chú minh bạch 'Không phải AI sinh' — bảo đảm tính trung thực tuyệt đối trong thẩm định và chấm thi."
        )
    ]
    for title_m, desc_m in llm_modes:
        p_m = doc.add_paragraph()
        r_m = p_m.add_run(f"• {title_m}: ")
        r_m.bold = True
        r_m.font.color.rgb = RGBColor(30, 58, 138)
        p_m.add_run(desc_m)
        p_m.paragraph_format.space_after = Pt(4)

    doc.add_heading("2.3. Bước Tiến Vượt Trội: Financial Safety Gates & Verifier", level=2)
    doc.add_paragraph(
        "Trong sơ đồ Agentic AI thông thường, sau khi hoàn thành vòng lặp, Agent sẽ 'Take Action' trực tiếp "
        "(ví dụ: tự động đặt vé máy bay, tự sửa lịch trình). "
        "Tuy nhiên, TRONG LĨNH VỰC TÀI CHÍNH VÀ THANH TOÁN, ĐÂY LÀ ĐIỀU CỰC KỲ NGUY HIỂM vì LLM có thể ảo giác (Hallucination) hoặc bị lừa."
    )

    add_callout_box(
        doc,
        [
            "Agent AI KHÔNG BAO GIỜ được cấp quyền ghi trực tiếp (No Direct Writes): AI chỉ có quyền tạo Đề xuất hành động (ActionProposal với status = PROPOSED).",
            "Chính sách thẩm định (Policy Engine): Kiểm tra tiền điều kiện số dư, hạn mức hoàn tiền, quyền hạn.",
            "Phê duyệt có chữ ký (Approval HMAC): Các lệnh tài chính nhạy cảm phải được ký số xác thực bởi con người có thẩm quyền (Support, Ops, Finance, Manager).",
            "Thực thi có khóa (Safe Execution): Sử dụng SELECT FOR UPDATE khóa hàng trên PostgreSQL.",
            "Xác minh độc lập 2 pha (Read-Back Verifier): Mở một kết nối CSDL hoàn toàn mới để đọc lại trạng thái thật. Chỉ khi hậu điều kiện PASSED, vụ việc mới được chuyển sang RESOLVED."
        ],
        title="BƯỚC TIẾN CỦA FINRESOLVE AI SO VỚI AGENTIC AI TIÊU CHUẨN",
        border_color="D97706",
        bg_color="FFFBEB"
    )

    # -------------------------------------------------------------
    # SECTION 3: CẤU TRÚC DATABASE & SƠ ĐỒ ERD
    # -------------------------------------------------------------
    h3 = doc.add_heading("3. CẤU TRÚC CƠ SỞ DỮ LIỆU CHI TIẾT & SƠ ĐỒ ERD", level=1)
    h3.paragraph_format.space_before = Pt(16)
    h3.paragraph_format.space_after = Pt(6)

    doc.add_paragraph(
        "Cơ sở dữ liệu của FinResolve AI được thiết kế trên chuẩn PostgreSQL 16 (quản lý bằng SQLAlchemy 2 và Alembic), "
        "gồm 19 bảng thực thể được phân loại thành 6 nhóm chức năng chặt chẽ:"
    )

    group_summary = [
        ("Nhóm 1: Phiên & Cô lập dữ liệu", "demo_sessions", "Quản lý phiên guest sandbox, cấp phát token dsk_..., áp đặt quota số lượng vụ việc và thời gian sống (TTL)."),
        ("Nhóm 2: Vụ việc & Bằng chứng", "cases, evidence, case_events", "Lưu trữ vòng đời khiếu nại, máy trạng thái, các loại bằng chứng (CLAIM, OBSERVED_FACT) và nhật ký kiểm toán bất biến (Append-only Audit Log)."),
        ("Nhóm 3: Đề xuất & Xác minh", "action_proposals, approvals, action_executions, verifications", "Quản lý các hành động ghi có kiểm soát, cơ chế duyệt phân quyền có chữ ký số HMAC và tiến trình đọc lại xác minh 2 pha độc lập."),
        ("Nhóm 4: Trung tâm Rủi ro", "risk_signals", "Ghi nhận các tín hiệu bất thường, lạm dụng chính sách, tái sử dụng giao dịch và cảnh báo tạm giữ hành động (HOLD_ACTION)."),
        ("Nhóm 5: Đơn hàng & Kho bãi", "sim_orders, order_items, products, warehouses, inventory_levels, inventory_reservations, inventory_movements", "Mô phỏng đầy đủ dữ liệu thương mại điện tử, giỏ hàng, hạn mức tồn kho và đặt trước hàng hóa."),
        ("Nhóm 6: Thanh toán & Hoàn tiền", "sim_payments, sim_refunds, sim_payment_events", "Đóng vai trò sổ cái sự thật (Ledger of Truth), ghi nhận biến động giao dịch MoMo/Bank, hạn mức hoàn tiền khả dụng."),
    ]
    t_groups = doc.add_table(rows=len(group_summary) + 1, cols=3)
    t_groups.rows[0].cells[0].paragraphs[0].text = "Nhóm chức năng"
    t_groups.rows[0].cells[1].paragraphs[0].text = "Bảng CSDL liên quan"
    t_groups.rows[0].cells[2].paragraphs[0].text = "Mô tả vai trò nghiệp vụ"
    t_groups.rows[0].cells[0].width = Inches(1.8)
    t_groups.rows[0].cells[1].width = Inches(2.0)
    t_groups.rows[0].cells[2].width = Inches(2.7)
    for i, (g, tbls, desc) in enumerate(group_summary, start=1):
        r = t_groups.rows[i]
        r.cells[0].paragraphs[0].text = g
        r.cells[1].paragraphs[0].text = tbls
        r.cells[2].paragraphs[0].text = desc
    format_table(t_groups)

    doc.add_paragraph().paragraph_format.space_after = Pt(8)

    doc.add_heading("3.1. Chi tiết Cấu trúc Các Bảng Trọng Tâm", level=2)

    tables_detail = [
        {
            "name": "1. cases (Bảng Vụ việc / Khiếu nại)",
            "desc": "Thực thể trung tâm lưu trữ yêu cầu xử lý từ khách hàng hoặc sự kiện bất thường.",
            "cols": [
                ("id", "VARCHAR(64)", "Khóa chính (PK), định dạng UUID"),
                ("session_id", "VARCHAR(64)", "Khóa ngoại (FK) -> demo_sessions.id (Cách ly dữ liệu phiên)"),
                ("type", "VARCHAR(64)", "Loại vụ việc: PAYMENT_DISPUTE, REFUND_REQUEST, SYNC_ERROR"),
                ("goal", "VARCHAR(512)", "Mục tiêu điều tra của Agent"),
                ("state", "VARCHAR(32)", "Trạng thái: NEW, INVESTIGATING, WAITING_EVIDENCE, WAITING_APPROVAL, EXECUTING, VERIFYING, RESOLVED, UNKNOWN_OUTCOME, REJECTED"),
                ("version", "INTEGER", "Khóa lạc quan (Optimistic Concurrency Control), tự tăng +1"),
                ("idempotency_key", "VARCHAR(128)", "Khóa chống ghi trùng lặp từ Client (X-Idempotency-Key)")
            ]
        },
        {
            "name": "2. evidence (Bảng Bằng chứng điều tra)",
            "desc": "Lưu trữ mọi dữ kiện thu thập được trong quá trình tiếp nhận và điều tra vụ việc.",
            "cols": [
                ("id", "VARCHAR(64)", "Khóa chính (PK)"),
                ("case_id", "VARCHAR(64)", "Khóa ngoại (FK) -> cases.id"),
                ("kind", "VARCHAR(32)", "Phân loại: CLAIM (lời khai khách), OBSERVED_FACT (dữ kiện sổ cái MoMo), HYPOTHESIS, UNKNOWN"),
                ("source", "VARCHAR(64)", "Nguồn gốc: CUSTOMER_INPUT, MOMO_QUERY_API, OMS_DATABASE"),
                ("source_ref", "VARCHAR(128)", "Mã tham chiếu: TxnID, OrderID, Ảnh ID"),
                ("confidence", "FLOAT", "Độ tin cậy: CLAIM = 0.5; OBSERVED_FACT = 1.0")
            ]
        },
        {
            "name": "3. action_proposals & approvals (Đề xuất & Phê duyệt)",
            "desc": "Kiểm soát các lệnh ghi nhạy cảm (Cập nhật trạng thái đơn, Hoàn tiền).",
            "cols": [
                ("id", "VARCHAR(64)", "Khóa chính đề xuất (PK)"),
                ("action_type", "VARCHAR(64)", "SYNC_PAYMENT_STATUS, REFUND_EXECUTE, HOLD_ORDER"),
                ("params_json", "JSON", "Tham số thực thi: order_id, amount, payment_id"),
                ("risk_level", "VARCHAR(32)", "Mức độ rủi ro: LOW, MEDIUM, HIGH, CRITICAL"),
                ("digest", "VARCHAR(128)", "Chữ ký mật mã HMAC v2 bảo đảm không bị giả mạo tham số"),
                ("expiry", "DATETIME", "Thời hạn hiệu lực của chữ ký phê duyệt")
            ]
        },
        {
            "name": "4. verifications (Bảng Xác minh Hậu điều kiện)",
            "desc": "Chứng cứ chứng minh kết quả sau khi thực hiện lệnh ghi, độc lập với hàm thực thi.",
            "cols": [
                ("id", "VARCHAR(64)", "Khóa chính (PK)"),
                ("case_id", "VARCHAR(64)", "Khóa ngoại (FK) -> cases.id"),
                ("action_execution_id", "VARCHAR(64)", "Khóa ngoại (FK) -> action_executions.id"),
                ("postconditions_json", "JSON", "Điều kiện bắt buộc đã đạt"),
                ("verdict", "VARCHAR(32)", "Phán quyết: PASSED (Hợp lệ hoàn toàn), FAILED, INCONCLUSIVE")
            ]
        },
        {
            "name": "5. sim_payments & sim_refunds (Sổ cái Thanh toán & Hoàn tiền)",
            "desc": "Nguồn sự thật tài chính (Source of Financial Truth) để đối soát mọi giao dịch.",
            "cols": [
                ("id", "VARCHAR(64)", "Khóa chính giao dịch (PK)"),
                ("provider_transaction_id", "VARCHAR(128)", "Mã giao dịch MoMo thật do cổng thanh toán cấp"),
                ("amount", "BIGINT", "Số tiền thanh toán thực tế (VND)"),
                ("status", "VARCHAR(32)", "Trạng thái giao dịch: SUCCESS, PENDING, FAILED"),
                ("refundable_amount", "BIGINT (Computed)", "Hạn mức hoàn tiền khả dụng = amount - settled_refunds - pending_refunds")
            ]
        }
    ]

    for td in tables_detail:
        p_name = doc.add_paragraph()
        r_name = p_name.add_run(td["name"])
        r_name.bold = True
        r_name.font.size = Pt(11)
        r_name.font.color.rgb = RGBColor(30, 58, 138)
        p_desc = doc.add_paragraph(td["desc"])
        p_desc.paragraph_format.space_after = Pt(4)

        t_sub = doc.add_table(rows=len(td["cols"]) + 1, cols=3)
        t_sub.rows[0].cells[0].paragraphs[0].text = "Tên trường (Column)"
        t_sub.rows[0].cells[1].paragraphs[0].text = "Kiểu dữ liệu (Data Type)"
        t_sub.rows[0].cells[2].paragraphs[0].text = "Mô tả & Ràng buộc toàn vẹn"
        t_sub.rows[0].cells[0].width = Inches(1.8)
        t_sub.rows[0].cells[1].width = Inches(1.5)
        t_sub.rows[0].cells[2].width = Inches(3.2)
        for idx, (col_name, col_type, col_desc) in enumerate(td["cols"], start=1):
            r = t_sub.rows[idx]
            r.cells[0].paragraphs[0].text = col_name
            r.cells[1].paragraphs[0].text = col_type
            r.cells[2].paragraphs[0].text = col_desc
        format_table(t_sub, header_bg="334155")
        doc.add_paragraph().paragraph_format.space_after = Pt(8)

    # -------------------------------------------------------------
    # SECTION 4: PHÂN TÍCH CA THỰC TẾ: BILL GIẢ (FAKE BILL DEFENSE)
    # -------------------------------------------------------------
    h4 = doc.add_heading("4. PHÂN TÍCH CA THỰC TẾ: CƠ CHẾ PHÒNG THỦ & XỬ LÝ BILL GIẢ (FAKE BILL)", level=1)
    h4.paragraph_format.space_before = Pt(16)
    h4.paragraph_format.space_after = Pt(6)

    doc.add_paragraph(
        "Kịch bản gian lận: Kẻ gian mua đơn hàng 500.000đ (Mã 'ORD-SYNC-999'), không thanh toán nhưng dùng phần mềm "
        "tạo ảnh biên lai MoMo giả mạo ghi nhận đã chuyển 500.000đ với mã giao dịch bịa đặt 'MOMO_TXN_FAKE_888' "
        "rồi gửi CSKH yêu cầu duyệt giao hàng gấp."
    )

    fake_steps = [
        (
            "Bước 1: Tiếp nhận (Intake Gate)",
            "Hệ thống nhận qua POST /api/v1/cases. Tạo Case trạng thái NEW, version: 1. "
            "Toàn bộ tin nhắn và mã giao dịch do khách cung cấp được lưu vào bảng evidence dưới dạng kind = 'CLAIM', confidence = 0.5. "
            "BẤT BIẾN KỸ THUẬT: Tuyệt đối không bao giờ tự động chuyển đơn sang PAID chỉ dựa trên CLAIM."
        ),
        (
            "Bước 2: Phân tích Ý định & Chống Hack",
            "understanding.py trích xuất thực thể: order_id = 'ORD-SYNC-999', claimed_payment_ref = 'MOMO_TXN_FAKE_888', claimed_amount = 500.000đ. "
            "Đồng thời quét lọc mã độc Prompt Injection, ngăn chặn câu lệnh chiếm quyền."
        ),
        (
            "Bước 3: Điều tra Sổ cái Độc lập (Active Read Tools)",
            "Agent kích hoạt công cụ read_tools.py: get_order('ORD-SYNC-999') và find_payment_candidates('ORD-SYNC-999'). "
            "Truy vấn trực tiếp vào Sổ cái MoMo thật: Không tìm thấy giao dịch nào mang mã MOMO_TXN_FAKE_888! "
            "Công cụ trả về ToolResult(status='NOT_FOUND') và lưu vào evidence dưới dạng OBSERVED_FACT (độ tin cậy = 1.0)."
        ),
        (
            "Bước 4: Chốt chặn Rủi ro & Từ chối Thực thi",
            "Mâu thuẫn giữa CLAIM và OBSERVED_FACT kích hoạt ngay Risk Engine: "
            "Ghi nhận RiskSignal(category='SUSPECTED_ABUSE', level='HIGH', response='HOLD_ACTION'). "
            "Policy Engine kiểm tra tiền điều kiện: Bắt buộc phải có thanh toán SUCCESS. Do không thỏa mãn, Policy Engine TỪ CHỐI đề xuất đồng bộ."
        ),
        (
            "Bước 5: Đầu ra & Nhật ký Kiểm toán (Output & Audit)",
            "• Trạng thái Case: Chuyển sang WAITING_EVIDENCE hoặc REJECTED. Cấm RESOLVED. "
            "• Trạng thái Đơn: Giữ nguyên UNPAID, kho không trừ hàng, không xuất kho. "
            "• Phản hồi khách: Khách quan thông báo chưa nhận được tiền từ MoMo, yêu cầu đối soát lại. "
            "• Audit Trail: case_events lưu toàn bộ chứng cứ điều tra phục vụ hậu kiểm."
        )
    ]

    t_fake = doc.add_table(rows=len(fake_steps) + 1, cols=2)
    t_fake.rows[0].cells[0].paragraphs[0].text = "Bước xử lý"
    t_fake.rows[0].cells[1].paragraphs[0].text = "Diễn biến kỹ thuật & Cơ chế bảo vệ"
    t_fake.rows[0].cells[0].width = Inches(2.0)
    t_fake.rows[0].cells[1].width = Inches(4.5)
    for i, (st, desc) in enumerate(fake_steps, start=1):
        r = t_fake.rows[i]
        r.cells[0].paragraphs[0].text = st
        r.cells[1].paragraphs[0].text = desc
    format_table(t_fake, header_bg="991B1B")

    doc.add_paragraph().paragraph_format.space_after = Pt(12)

    # -------------------------------------------------------------
    # SECTION 5: HƯỚNG DẪN TRIỂN KHAI & PRODUCTION ROADMAP
    # -------------------------------------------------------------
    h5 = doc.add_heading("5. HƯỚNG DẪN TRIỂN KHAI & LỘ TRÌNH PRODUCTION", level=1)
    h5.paragraph_format.space_before = Pt(16)
    h5.paragraph_format.space_after = Pt(6)

    doc.add_paragraph(
        "• Triển khai Docker: docker compose -f infra/docker-compose.yml up --build (Web: cổng 3000, API: cổng 8000, DB: cổng 5433)\n"
        "• Triển khai Local: alembic upgrade head -> uvicorn app.main:app (Backend) -> npm run dev (Frontend)\n"
        "• Chuyển sang Production: Thay thế sim_payments bằng MoMo Open API Gateway (/query, /refund) ký HMAC-SHA256, "
        "thêm hàng đợi Celery/Kafka bất đồng bộ, bảo mật định danh OAuth2/OIDC và AI Pipeline đa tầng tối ưu chi phí."
    )

    doc.add_paragraph().paragraph_format.space_after = Pt(20)

    p_end = doc.add_paragraph()
    p_end.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    r_end = p_end.add_run("--- HẾT BÁO CÁO KỸ THUẬT FINRESOLVE AI ---")
    r_end.font.italic = True
    r_end.font.size = Pt(10)
    r_end.font.color.rgb = RGBColor(100, 116, 139)

    try:
        doc.save(output_path)
        print(f"Report successfully saved to: {output_path}")
    except PermissionError:
        alt_path = output_path.replace(".docx", "_v2.docx")
        doc.save(alt_path)
        print(f"File was locked by Word. Report successfully saved to alternative path: {alt_path}")

if __name__ == "__main__":
    out_dir = r"D:\momo\FinResolve\docs"
    os.makedirs(out_dir, exist_ok=True)
    out_file = os.path.join(out_dir, "Bao_Cao_Kien_Truc_ERD_Van_Hanh_FinResolve.docx")
    generate_report(out_file)
