from app.features.chat.service import ChatService


def test_citation_report_flags_lines_without_known_refs() -> None:
    service = ChatService()
    report = service._citation_report(
        "This claim has a bracket but not a known source [unknown].\n"
        "This claim is bound to a known paper [paper_id=42].",
        ["paper_id=42"],
    )
    assert "缺少显式来源绑定" in report
    assert "known paper" not in report


def test_citation_report_accepts_known_url_refs() -> None:
    service = ChatService()
    report = service._citation_report(
        "This current factual claim is linked to the web result [https://example.com/source].",
        ["https://example.com/source"],
    )
    assert report == ""
