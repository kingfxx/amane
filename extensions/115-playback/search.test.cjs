const { test } = require("node:test");
const assert = require("node:assert/strict");
const { candidates, masterUrl, parsePage } = require("./amane-115.user.js");

test("番号边界、分隔符、视频类型与候选去重", () => {
  const rows = [
    { n: "JUR-840-C-破解.mp4", pc: "good", s: "1024" },
    { n: "JUR_840-CD2.mkv", pc: "part2" },
    { n: "JUR-840-copy.mp4", pc: "good" },
    { n: "JUR-8400.mp4", pc: "wrong" },
    { n: "XJUR-840.mp4", pc: "wrong2" },
    { n: "JUR-840-trailer.mp4", pc: "trailer" },
    { n: "JUR-840.nfo", pc: "nfo" },
    { n: "JUR-840.mp4", pc: "bad&url" },
  ];
  assert.deepEqual(
    candidates(rows, "JUR-840").map((row) => row.pickcode),
    ["good", "part2"],
  );
  assert.equal(candidates([{ n: "FC2-PPV-1234567.mp4", pc: "fc2" }], "FC2-PPV-1234567").length, 1);
  assert.equal(candidates([{ n: "FC2-1234567.mp4", pc: "fc2" }], "FC2-PPV-1234567").length, 1);
  assert.equal(candidates([{ n: "FC2-12345678.mp4", pc: "fc2" }], "FC2-PPV-1234567").length, 0);
  assert.throws(() => candidates([], ""), /番号为空/);
  assert.throws(() => masterUrl("a&b"), /无效/);
  assert.equal(new URL(masterUrl("abc123")).searchParams.get("pick_code"), "abc123");
});

test("成功、空结果、登录错误与无效响应分别处理", () => {
  assert.deepEqual(parsePage({ state: true, data: [], count: "0" }), { rows: [], count: 0 });
  for (const [body, expected] of [
    [{ state: false, errno: 99 }, /登录/],
    [{ state: false, errno: 999 }, /受到限制/],
    [{ data: null }, /格式异常/],
    [{ data: [null] }, /格式异常/],
  ])
    assert.throws(() => parsePage(body), expected);
});
