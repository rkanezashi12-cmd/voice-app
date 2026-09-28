import assert from "node:assert/strict";
import { test } from "node:test";

import { chunkKey, MemoryStore, NonRetryableError, UploadQueue } from "../../web/recorder/lib/upload-queue.js";

const item = (seq) => ({ key: chunkKey("rec1", "1727488500123-a-abc123", seq), seq, data: new ArrayBuffer(4) });

test("chunkKey は辞書順が録音順になる", () => {
  const keys = [10, 2, 1].map((s) => chunkKey("r", "s", s)).sort();
  assert.deepEqual(keys, ["r/s/000001", "r/s/000002", "r/s/000010"]);
});

test("順番に送り、成功したものを端末から消す", async () => {
  const store = new MemoryStore();
  const sent = [];
  const q = new UploadQueue({ store, uploader: async (it) => sent.push(it.seq), sleep: async () => {} });
  await q.start();
  await q.add(item(1));
  await q.add(item(0));
  assert.equal(await q.drain(2000, 5), true);
  await q.stop();
  assert.deepEqual(sent, [0, 1]);
  assert.equal((await store.list()).length, 0);
  assert.equal(q.stats.uploaded, 2);
});

test("失敗したら待ってから再送する", async () => {
  const store = new MemoryStore();
  let calls = 0;
  const waits = [];
  const q = new UploadQueue({
    store,
    uploader: async () => {
      calls += 1;
      if (calls < 3) throw new Error("network");
    },
    sleep: async (ms) => {
      waits.push(ms);
    },
    baseBackoffMs: 100,
  });
  await q.start();
  await q.add(item(0));
  assert.equal(await q.drain(2000, 5), true);
  await q.stop();
  assert.equal(calls, 3);
  assert.deepEqual(waits.slice(0, 2), [100, 200]);
  assert.equal(q.stats.failedAttempts, 2);
});

test("再送できないエラー（期限切れなど）では止まって知らせる", async () => {
  const store = new MemoryStore();
  let fatal = null;
  const q = new UploadQueue({
    store,
    uploader: async () => {
      throw new NonRetryableError("expired");
    },
    onFatal: (e) => (fatal = e),
    sleep: async () => {},
  });
  await q.start();
  await q.add(item(0));
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(fatal.message, "expired");
  assert.equal((await store.list()).length, 1, "送れなかったものは端末に残す");
});

test("再起動後も端末に残ったものを送る", async () => {
  const store = new MemoryStore();
  await store.put(item(0));
  await store.put(item(1));
  const sent = [];
  const q = new UploadQueue({ store, uploader: async (it) => sent.push(it.seq), sleep: async () => {} });
  await q.start();
  assert.equal(await q.drain(2000, 5), true);
  await q.stop();
  assert.deepEqual(sent, [0, 1]);
});
