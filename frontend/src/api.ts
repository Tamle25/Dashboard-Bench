import type {
  Bench, LeaderRow, ModelEndpoint, ModelInput, ResultDetail, ResultRow, Run, RunInput,
  SamplePage, Source, SourceInput, SourceTest, Stats,
} from "./types";

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

// Lỗi 422 của FastAPI chỉ có `msg` kiểu "String should match pattern '…'", không nói ô nào
// sai. Ghép thêm nhãn lấy từ `loc` để người dùng biết phải sửa ở đâu.
const FIELD_LABEL: Record<string, string> = {
  id: "Tên hiển thị", agent: "Agent Harbor", model_name: "Model", base_url: "Base URL",
  secret_name: "Tên bí mật chứa API key", note: "Ghi chú", role: "Vai trò", kind: "Kiểu kết nối",
  url: "URL repo", ref: "Nhánh/tag/commit", manifest_path: "Đường dẫn manifest",
  bench_id: "Bộ bench", models: "Thí sinh", judge_id: "Giám khảo", samples: "Số mẫu/task",
};

function detailMsg(d: { msg: string; loc?: (string | number)[] }): string {
  const field = d.loc?.filter((x) => x !== "body" && x !== "query").pop();
  const label = typeof field === "string" ? FIELD_LABEL[field] || field : undefined;
  return label ? `${label}: ${d.msg}` : d.msg;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") msg = body.detail;
      else if (Array.isArray(body.detail)) msg = body.detail.map(detailMsg).join("; ");
    } catch {
      /* phản hồi không phải JSON */
    }
    throw new ApiError(msg, res.status);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

const post = <T>(path: string, body?: unknown) => req<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  stats: () => req<Stats>("/api/stats"),

  sources: () => req<Source[]>("/api/sources"),
  testSource: (b: SourceInput) => post<SourceTest>("/api/sources/test", b),
  createSource: (b: SourceInput) => post<Source>("/api/sources", b),
  updateSource: (id: number, b: Partial<SourceInput>) => req<Source>(`/api/sources/${id}`, { method: "PATCH", body: JSON.stringify(b) }),
  syncSource: (id: number) => post<{ ok: boolean }>(`/api/sources/${id}/sync`),

  benches: () => req<Bench[]>("/api/benches"),

  models: () => req<ModelEndpoint[]>("/api/models"),
  createModel: (b: ModelInput) => post<ModelEndpoint>("/api/models", b),
  pingModel: (id: string) => post<ModelEndpoint>(`/api/models/${encodeURIComponent(id)}/ping`),

  createRun: (b: RunInput) => post<Run>("/api/runs", b),
  activeRun: () => req<Run | null>("/api/runs/active"),
  run: (id: number) => req<Run>(`/api/runs/${id}`),
  cancelRun: (id: number) => post<{ ok: boolean }>(`/api/runs/${id}/cancel`),
  streamUrl: (id: number) => `/api/runs/${id}/stream`,

  results: (benchId?: number) => req<ResultRow[]>(`/api/results${benchId ? `?bench_id=${benchId}` : ""}`),
  leaderboard: (benchId: number) => req<LeaderRow[]>(`/api/leaderboard?bench_id=${benchId}`),
  result: (id: number) => req<ResultDetail>(`/api/results/${id}`),
  samples: (id: number, filter: string, offset = 0) => req<SamplePage>(`/api/results/${id}/samples?filter=${filter}&offset=${offset}&limit=50`),
  exportUrl: (id: number) => `/api/results/${id}/export`,
};

export const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));
