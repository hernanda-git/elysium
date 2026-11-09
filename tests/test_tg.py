from __future__ import annotations

from datetime import datetime, timezone

from elys.tgfeed import classify, parse_page, parse_post, parse_symbol, strip_html

TOKEN = "0x53ec6d672543828dfc47f8cd1d36d86f3a4483a6"
WALLET = "0x9041a814ded6d207f8ea264024d32a41e23c8b43"
TX = "0x" + "ab" * 32

WHALE_HTML = (
    "<b>Whale · &#036;24.25M buy of KNTQ</b><br/>"
    f'<a href="https://elysium.eco/screener/w/{WALLET}">0x9041…8b43</a> bought 68.17M KNTQ at &#036;0.3557'
)

SMART_HTML = (
    "<b>Smart money · 3 smart wallets bought HYPEPE</b><br/>"
    "&#036;2.9K in 27m · "
    f'<a href="https://elysium.eco/screener/w/{WALLET}">0xbc4a…00a6</a> &#036;1.8K'
)


def test_strip_html_unescapes_and_breaks():
    assert strip_html("a<br/>b &#036;5") == "a\nb $5"


def test_classify():
    assert classify("Whale · $1M buy of X") == "whale"
    assert classify("Smart money · 3 smart wallets bought X") == "smart_money"
    assert classify("Rug warning · CLU: creator wiped") == "rug_warning"
    assert classify("Launch · X drew 33 buyers") == "launch"
    assert classify("Bridge · $100K of HYPE landed") == "bridge"
    assert classify("hello") == "other"


def test_parse_symbol():
    assert parse_symbol("Whale · $24.25M buy of KNTQ\n0x90 bought", "whale") == "KNTQ"
    assert parse_symbol("Smart money · 3 smart wallets bought HYPEPE", "smart_money") == "HYPEPE"
    assert parse_symbol("Launch · ORIGAMI drew 33 buyers", "launch") == "ORIGAMI"
    assert parse_symbol("Rug warning · CLU: creator wiped 77 holders", "rug_warning") == "CLU"


def test_parse_post_extracts_entities():
    links = [
        f"https://elysium.eco/screener/{TOKEN}",
        f"https://elysium.eco/screener/w/{WALLET}",
        f"https://elysium.eco/tx/{TX}",
    ]
    post = parse_post(
        "elysiumecoalerts",
        195,
        datetime(2026, 11, 9, 20, 10, 54, tzinfo=timezone.utc),
        strip_html(WHALE_HTML),
        links,
    )
    assert post["kind"] == "whale"
    assert post["symbol"] == "KNTQ"
    assert post["token"] == TOKEN
    assert post["wallets"] == [WALLET]
    assert post["tx"] == TX
    assert post["ts"] == 1794255054


def test_parse_page_splits_messages():
    page = (
        '<div id="messages">'
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        '<div class="tgme_widget_message text_not_supported_wrap js-widget_message" '
        'data-post="elysiumecoalerts/190">'
        '<div class="tgme_widget_message_bubble">'
        f'<div class="tgme_widget_message_text">'
        f'<b>Whale</b> buy of KNTQ <a href="https://elysium.eco/screener/{TOKEN}">token</a>'
        f'</div><time datetime="2026-11-09T19:35:00+00:00"></time></div></div></div>'
        '<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        '<div class="tgme_widget_message js-widget_message" data-post="elysiumecoalerts/191">'
        '<div class="tgme_widget_message_text">'
        '<b>Launch · FRENS drew 27 buyers</b><br/>10m old on Montra'
        '</div><time datetime="2026-11-09T20:56:00+00:00"></time></div></div>'
        "</div>"
    )
    posts = parse_page("elysiumecoalerts", page)
    assert [p["id"] for p in posts] == [190, 191]
    assert posts[0]["token"] == TOKEN
    assert posts[1]["kind"] == "launch"
    assert posts[1]["symbol"] == "FRENS"


def test_parse_post_smart_money_wallets():
    post = parse_post(
        "elysiumecoalerts", 196, None, strip_html(SMART_HTML), [f"https://elysium.eco/screener/w/{WALLET}"]
    )
    assert post["kind"] == "smart_money"
    assert post["wallets"] == [WALLET]
    assert post["symbol"] == "HYPEPE"
