from email.message import EmailMessage

import pytest

from eta_agent.email_ingest import parse_eml

ACME = "sales@acme-passive.com.tw"
DATE = "Mon, 26 Oct 2026 10:00:00 +0800"
NEW = "您好，PUR-ORD-2026-00001 AC-C100 2,000 pcs 確認如期 11/5 到貨。"
OLD = "請問 PUR-ORD-2026-00001 AC-R10K 10,000 pcs 能否提前至 11/1 到貨？"


def mail(body=None, html=None, subject="RE: PUR-ORD-2026-00001 交期", sender=ACME, mid="fmt@x",
         charset="utf-8", attachments=()):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"], m["Message-ID"] = sender, "purchasing@example.com", subject, DATE, f"<{mid}>"
    if body is not None:
        m.set_content(body, charset=charset, cte="base64" if charset != "utf-8" else None)
        if html is not None:
            m.add_alternative(html, subtype="html")
    else:
        m.set_content(html, subtype="html")
    for name, ctype, data, disposition in attachments:
        main, sub = ctype.split("/")
        m.add_attachment(data, maintype=main, subtype=sub, filename=name, disposition=disposition)
    return m.as_bytes()


QUOTE_STYLES = {
    "outlook_en": f"{NEW}\n\nBest regards\nAcme\n\n-----Original Message-----\nFrom: purchasing@example.com\n"
                  f"Sent: Friday, October 23, 2026 4:00 PM\nTo: sales@acme-passive.com.tw\nSubject: 交期\n\n{OLD}",
    "outlook_zh": f"{NEW}\n\n業務部\n\n寄件者: 採購部 <purchasing@example.com>\n寄件日期: 2026年10月23日 下午 04:00\n"
                  f"收件者: sales@acme-passive.com.tw\n主旨: 交期\n\n{OLD}",
    "outlook_underscore": f"{NEW}\n\n________________________________\nFrom: purchasing@example.com\n"
                          f"Sent: Friday, October 23, 2026 4:00 PM\n\n{OLD}",
    "gmail_en": f"{NEW}\n\nOn Fri, Oct 23, 2026 at 4:00 PM 採購部 <purchasing@example.com> wrote:\n> {OLD}\n>\n> 謝謝",
    "gmail_zh": f"{NEW}\n\n採購部 <purchasing@example.com> 於 2026年10月23日 週五 下午4:00寫道：\n> {OLD}",
    "angle_brackets": f"{NEW}\n\n> {OLD}\n> 謝謝",
    "zh_original": f"{NEW}\n\n----- 原始郵件 -----\n寄件者：purchasing@example.com\n\n{OLD}",
}


@pytest.mark.parametrize("style", sorted(QUOTE_STYLES))
def test_quoted_old_mail_is_separated(style):
    em = parse_eml(mail(QUOTE_STYLES[style]))
    assert "AC-C100" in em.body and "11/5" in em.body
    assert "AC-R10K" not in em.body and "11/1" not in em.body
    assert "AC-R10K" in em.quoted


@pytest.mark.parametrize("style", sorted(QUOTE_STYLES))
def test_quoted_old_mail_never_reaches_llm_or_changes_po(agent, erp, spy, style):
    r = agent.process_eml(mail(QUOTE_STYLES[style], mid=f"q-{style}@x"))
    sent = spy.calls[0]["body"]
    assert "AC-R10K" not in sent and "11/1" not in sent
    assert r["status"] == "done"
    assert erp.po_lines["POI-0001-2"]["schedule_date"] == "2026-11-05"


def test_reply_quoting_a_date_inside_the_new_part_is_kept():
    body = "PUR-ORD-2026-00001 AC-C100 改為 11/7 到貨。\n備註：數量 > 1,000 pcs 的部分分兩車。"
    em = parse_eml(mail(body))
    assert "11/7" in em.body and "分兩車" in em.body and em.quoted == ""


def test_reply_with_nothing_but_the_quote_goes_to_manual(agent, erp, spy):
    r = agent.process_eml(mail(f"\n\n-----Original Message-----\nFrom: purchasing@example.com\n\n{OLD}", mid="only-q@x"))
    assert r["status"] == "manual" and r["route"] == "stop"
    assert spy.calls == []
    assert erp.po_lines["POI-0001-2"]["schedule_date"] == "2026-11-05"


def test_forwarded_part_is_not_read_as_the_reply():
    em = parse_eml(mail(f"請參考下方工廠回覆。\n\n---------- Forwarded message ---------\nFrom: factory@acme\n\n{OLD}"))
    assert "AC-R10K" not in em.body and "AC-R10K" in em.quoted


TABLE = ("<html><body><p>您好，以下為交期回覆：</p>"
         "<table><tr><th>採購單</th><th>料號</th><th>數量</th><th>到貨日</th></tr>"
         "<tr><td>PUR-ORD-2026-00001</td><td>AC-C100</td><td>2,000&nbsp;pcs</td><td>11/7</td></tr></table>"
         "<p>Acme&nbsp;&amp;&nbsp;Co.</p></body></html>")


def test_html_table_becomes_one_readable_line_per_row():
    em = parse_eml(mail(html=TABLE))
    row = next(ln for ln in em.body.splitlines() if "PUR-ORD-2026-00001" in ln)
    assert "AC-C100" in row and "2,000 pcs" in row and "11/7" in row
    assert "&nbsp;" not in em.body and "&amp;" not in em.body and "Acme & Co." in em.body


def test_html_only_mail_is_processed(agent, erp, spy):
    r = agent.process_eml(mail(html=TABLE.replace("<td>11/7</td>", "<td>延後至 11/7 到貨</td>"), mid="html@x"))
    assert "PUR-ORD-2026-00001 AC-C100 2,000 pcs 延後至 11/7 到貨" in spy.calls[0]["body"]
    assert r["status"] == "done"
    assert erp.po_lines["POI-0001-1"]["schedule_date"] == "2026-11-07"


def test_html_table_with_date_kind_only_in_header_is_not_auto(agent, erp):
    r = agent.process_eml(mail(html=TABLE, mid="html2@x"))
    assert r["status"] != "done"
    assert erp.po_lines["POI-0001-1"]["schedule_date"] == "2026-11-05"


def test_html_quoted_block_is_separated():
    html = (f"<div>{NEW}</div><div class=\"gmail_quote\"><div>採購部 寫道：</div>"
            f"<blockquote>{OLD}</blockquote></div>")
    em = parse_eml(mail(html=html))
    assert "AC-C100" in em.body and "AC-R10K" not in em.body and "AC-R10K" in em.quoted


def test_outlook_html_reply_header_is_separated():
    html = (f"<div>{NEW}</div><hr><div id=\"divRplyFwdMsg\"><b>From:</b> purchasing@example.com<br>"
            f"<b>Sent:</b> Friday</div><div>{OLD}</div>")
    em = parse_eml(mail(html=html))
    assert "AC-R10K" not in em.body and "AC-R10K" in em.quoted


def test_big5_mail_is_decoded():
    body = "您好，PUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨，造成不便敬請見諒。"
    em = parse_eml(mail(body, subject="回覆：交期確認", charset="big5"))
    assert em.body == body and em.subject == "回覆：交期確認"


def test_big5_label_with_cp950_only_characters_is_decoded():
    body = "PUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨，運費 €20 由我方負擔。"
    m = EmailMessage()
    m["From"], m["Subject"], m["Date"] = ACME, "交期", DATE
    m["Content-Type"] = 'text/plain; charset="big5"'
    m["Content-Transfer-Encoding"] = "8bit"
    raw = m.as_bytes().split(b"\n\n")[0] + b"\n\n" + body.encode("cp950")
    em = parse_eml(raw)
    assert "€20" in em.body and "延後至 11/7" in em.body


PDF = b"%PDF-1.4 fake"


def test_attachment_names_are_recorded():
    em = parse_eml(mail("交期請參考附件。", attachments=[("交期表.pdf", "application/pdf", PDF, "attachment")]))
    assert em.attachments == ("交期表.pdf",)


def test_schedule_only_in_attachment_is_not_auto(agent, erp):
    r = agent.process_eml(mail("PUR-ORD-2026-00001 交期請參考附件，謝謝。", mid="att1@x",
                               attachments=[("交期表.pdf", "application/pdf", PDF, "attachment")]))
    assert r["status"] != "done"
    assert erp.po_lines["POI-0001-1"]["schedule_date"] == "2026-11-05"


def test_clear_reply_with_attachment_still_goes_to_review(agent, erp):
    r = agent.process_eml(mail("PUR-ORD-2026-00001 AC-C100 2,000 pcs 延後至 11/7 到貨。", mid="att2@x",
                               attachments=[("出貨通知.xlsx", "application/vnd.openxmlformats-officedocument"
                                             ".spreadsheetml.sheet", b"PK..", "attachment")]))
    assert r["status"] == "waiting_review"
    assert any("出貨通知.xlsx" in x for x in r["reasons"])
    assert erp.po_lines["POI-0001-1"]["schedule_date"] == "2026-11-05"


def test_signature_logo_does_not_block_auto(agent, erp):
    r = agent.process_eml(mail("PUR-ORD-2026-00001 AC-C100 2,000 pcs 延後至 11/7 到貨。", mid="logo@x",
                               attachments=[("logo.png", "image/png", b"\x89PNG", "inline")]))
    assert r["status"] == "done"


def test_one_mail_two_purchase_orders_each_matched_correctly(agent, erp):
    body = ("PUR-ORD-2026-00001 AC-C100 2,000 pcs 延後至 11/7 到貨。\n"
            "PUR-ORD-2026-00005 AC-BOX1 1,000 pcs 延後至 11/17 到貨。")
    r = agent.process_eml(mail(body, mid="two@x"))
    lines = {d["line"]: d["new_eta"] for d in r["state"]["diffs"]}
    assert lines == {"POI-0001-1": "2026-11-07", "POI-0005-1": "2026-11-17"}


def test_mail_forwarded_by_our_own_colleague_is_not_processed(agent, erp, spy):
    body = f"FYI\n\n---------- Forwarded message ---------\nFrom: {ACME}\n\nPUR-ORD-2026-00001 AC-C100 延後至 11/7 到貨。"
    r = agent.process_eml(mail(body, sender="buyer@example.com", subject="Fwd: 交期", mid="fwd@x"))
    assert r["route"] == "stop" and spy.calls == []
    assert erp.po_lines["POI-0001-1"]["schedule_date"] == "2026-11-05"
