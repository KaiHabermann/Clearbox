"""The reviewer reply parser (clearbox.reviews.parse_review) on its own.

No Clearbox server and no database. Every review, automatic or on demand, is
parsed by this one function, so a regression here changes what get_reviews and
rework see for both. Covers the asked-for shape, a reply with no VERDICT line,
text around the verdict block, and the older unlabelled paragraph shape.

Run:  uv run python tests/test_review_format.py
"""
from clearbox.reviews import NODE_REVIEW_SYSTEM_APPEND, PLAN_REVIEW_SYSTEM_APPEND, REVIEW_FORMAT, parse_review

# the shape REVIEW_FORMAT asks for
v, s, f = parse_review(
    "VERDICT: concerns\n"
    "SUMMARY: The parser drops the last line.\n"
    "FINDINGS:\n"
    "- parse.py:12 off by one\n"
    "- no test for empty input"
)
assert (v, s) == ("concerns", "The parser drops the last line."), (v, s)
assert f == "- parse.py:12 off by one\n- no test for empty input", f
print("well-formed: ok")

# "none" findings are no findings
assert parse_review("VERDICT: ok\nSUMMARY: Fine.\nFINDINGS:\nnone") == ("ok", "Fine.", None)
assert parse_review("VERDICT: ok\nSUMMARY: Fine.\nFINDINGS: - none") == ("ok", "Fine.", None)
print("none findings: ok")

# no VERDICT line: the reviewer could not form a verdict, the whole reply is kept as the summary
reply = "I looked at the diff.\n\nIt seems mostly fine but I am not sure."
assert parse_review(reply) == ("error", reply, None), parse_review(reply)
assert parse_review("") == ("error", "(reviewer produced no output)", None)
print("no verdict -> error: ok")

# working before the block, markdown around the labels, and the format line quoted earlier on:
# the last VERDICT line counts and what precedes it is dropped
v, s, f = parse_review(
    "I ran `pytest` (3 passed) and read the diff. The format asks for\n"
    "VERDICT: ok | concerns | error\n"
    "so here it is.\n\n"
    "**VERDICT:** ok\n"
    "**SUMMARY:** Does what the node asked.\n"
    "**FINDINGS:**\n"
    "- nit: a stray print in cli.py"
)
assert (v, s, f) == ("ok", "Does what the node asked.", "- nit: a stray print in cli.py"), (v, s, f)
print("extra text around the block: ok")

# unlabelled: first paragraph after the verdict is the summary, the rest the findings
v, s, f = parse_review("VERDICT: Concerns\nTwo problems.\n\n- a\n- b")
assert (v, s, f) == ("concerns", "Two problems.", "- a\n- b"), (v, s, f)
print("unlabelled paragraphs: ok")

# both system prompts end with the one format and take the plan's tests note
for name, text in (("node", NODE_REVIEW_SYSTEM_APPEND), ("plan", PLAN_REVIEW_SYSTEM_APPEND)):
    assert text.endswith(REVIEW_FORMAT) and "{tests}" in text, name
    text.format(tests="x")  # no other placeholders
print("system prompts: ok")
print("review format OK")
