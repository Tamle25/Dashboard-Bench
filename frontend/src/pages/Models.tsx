import { useState } from "react";
import { api, errMsg } from "../api";
import { MODEL_STATUS, ROLE_TEXT } from "../format";
import { useApi } from "../hooks";
import type { ModelEndpoint, ModelInput, Role } from "../types";
import { Badge, ErrorNotice, Loading, PageHead } from "../ui";

const COLS = "minmax(0, 1.1fr) 112px minmax(0, 0.9fr) minmax(0, 1.4fr) 132px 72px 104px";
const EMPTY: ModelInput = { id: "", note: "", role: "contestant", kind: "openai", base_url: "", model_name: "", secret_name: "", agent: "" };
const KIND_TEXT: Record<ModelEndpoint["kind"], string> = { openai: "OpenAI-compatible", mock: "Mock (thử nghiệm)", harbor: "Agent Harbor" };

export default function ModelsPage() {
  const models = useApi(api.models, []);
  const [checking, setChecking] = useState<Set<string>>(new Set());
  const [form, setForm] = useState<ModelInput>(EMPTY);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string>();
  const set = (p: Partial<ModelInput>) => setForm((f) => ({ ...f, ...p }));

  const replace = (m: ModelEndpoint) => models.setData((list) => (list || []).map((x) => (x.id === m.id ? m : x)));

  async function ping(id: string) {
    setChecking((s) => new Set(s).add(id));
    try {
      replace(await api.pingModel(id));
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setChecking((s) => { const n = new Set(s); n.delete(id); return n; });
    }
  }

  async function add() {
    setAdding(true);
    setError(undefined);
    try {
      const m = await api.createModel({
        ...form,
        id: form.id.trim(),
        base_url: form.kind === "mock" ? form.base_url.trim() || "mock://0.7" : form.kind === "harbor" ? "" : form.base_url.trim(),
        model_name: form.model_name?.trim() || null,
        secret_name: form.secret_name?.trim() || null,
        agent: form.agent?.trim() || null,
      });
      models.setData((list) => [...(list || []), m].sort((a, b) => a.id.localeCompare(b.id)));
      setForm(EMPTY);
    } catch (e) {
      setError(errMsg(e));
    } finally {
      setAdding(false);
    }
  }

  const canAdd = !!form.id.trim() && !adding && (
    form.kind === "mock" || (form.kind === "harbor" ? !!form.agent?.trim() : !!form.base_url.trim())
  );

  return (
    <div className="page">
      <PageHead title="Thí sinh & giám khảo"
        lead="Đăng ký endpoint của từng mô hình. Mô hình có vai trò Thí sinh sẽ hiện ở bước 2, vai trò Giám khảo hiện ở bước 3 của màn Chạy mới." />

      <section aria-labelledby="mdl-h" className="card flush-bottom">
        <h2 id="mdl-h" className="card-title">Mô hình đã đăng ký</h2>
        {models.error && <ErrorNotice error={models.error} retry={models.reload} />}
        <div className="table-scroll">
          <div style={{ minWidth: 980 }}>
            <div className="trow thead" style={{ gridTemplateColumns: COLS }}>
              <span>Tên</span><span>Vai trò</span><span>Kết nối</span><span>Endpoint · bí mật</span><span>Trạng thái</span><span className="right">Độ trễ</span><span />
            </div>
            {models.loading && !models.data && <Loading />}
            {models.data?.length === 0 && <p className="muted" style={{ margin: 12 }}>Chưa có mô hình nào. Thêm ở bên dưới.</p>}
            {models.data?.map((m) => {
              const busy = checking.has(m.id);
              const st = MODEL_STATUS[busy ? "checking" : m.status];
              return (
                <div key={m.id} className="trow" style={{ gridTemplateColumns: COLS, minHeight: 56, padding: "6px 12px" }}>
                  <span style={{ minWidth: 0 }}>
                    <span className="mono" style={{ display: "block", fontWeight: 600, fontSize: 13 }}>{m.id}</span>
                    <span className="muted" style={{ display: "block", fontSize: 12 }}>{m.note}</span>
                  </span>
                  <span><span className="tag">{ROLE_TEXT[m.role]}</span></span>
                  <span style={{ fontSize: 13, color: "var(--text-2)" }}>
                    {KIND_TEXT[m.kind]}
                    {(m.kind === "harbor" || m.agent) && <span className="mono muted" style={{ display: "block", fontSize: 12 }}>agent {m.agent}</span>}
                  </span>
                  <span style={{ minWidth: 0 }}>
                    <span className="mono ellipsis" style={{ display: "block", fontSize: 12.5 }} title={m.base_url}>
                      {m.kind === "harbor" ? (m.model_name ? `model ${m.model_name}` : "agent tự chọn model") : m.base_url}
                    </span>
                    <span className="mono" style={{ display: "block", fontSize: 12, color: m.secret_present === false ? "var(--err)" : "var(--muted)" }}>
                      {m.secret_name ? `bí mật: ${m.secret_name}${m.secret_present === false ? " (chưa đặt)" : ""}` : "không cần API key"}
                    </span>
                  </span>
                  <span title={m.last_error || undefined} style={{ minWidth: 0 }}>
                    <Badge text={st.text} tone={st.tone} dot />
                    {m.status === "down" && m.last_error && <span style={{ display: "block", fontSize: 12, color: "var(--err)", marginTop: 2 }}>{m.last_error}</span>}
                  </span>
                  <span className="right num" style={{ fontSize: 13, color: "var(--text-2)" }}>{m.latency_ms != null ? `${m.latency_ms} ms` : "—"}</span>
                  <button type="button" className="btn btn-sm" disabled={busy} onClick={() => ping(m.id)} aria-label={`Kiểm tra kết nối ${m.id}`}>
                    {busy ? "Đang thử…" : "Kiểm tra"}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </section>

      <section aria-labelledby="addm-h" className="card stack" style={{ gap: 16 }}>
        <h2 id="addm-h" className="card-title">Thêm mô hình</h2>
        <div className="fields">
          <label className="field">Tên hiển thị
            <input className="input mono" value={form.id} placeholder="vd. vina-chat-v4" spellCheck={false} onChange={(e) => set({ id: e.target.value })} />
          </label>
          <label className="field">Vai trò
            <select className="input" value={form.role} onChange={(e) => set({ role: e.target.value as Role })}>
              <option value="contestant">Thí sinh</option>
              <option value="judge">Giám khảo</option>
              <option value="both">Cả hai</option>
            </select>
          </label>
          <label className="field">Kiểu kết nối
            <select className="input" value={form.kind} onChange={(e) => set({ kind: e.target.value as ModelInput["kind"] })}>
              <option value="openai">OpenAI-compatible (vLLM, TGI, API…)</option>
              <option value="harbor">Agent Harbor (oracle, claude-code, codex…)</option>
              <option value="mock">Mock (thử luồng, không gọi mạng)</option>
            </select>
          </label>
          <label className="field">Ghi chú
            <input className="input" value={form.note} placeholder="vd. checkpoint 10/11" onChange={(e) => set({ note: e.target.value })} />
          </label>
          {form.kind !== "harbor" && (
            <label className="field full">{form.kind === "mock" ? "Chất lượng mock" : "Base URL"}
              <input className="input mono" value={form.base_url} spellCheck={false}
                placeholder={form.kind === "mock" ? "mock://0.8  (đúng ~80% mẫu)" : "http://host:8000/v1"} onChange={(e) => set({ base_url: e.target.value })} />
            </label>
          )}
          {form.kind !== "mock" && (
            <label className="field">{form.kind === "harbor" ? "Agent Harbor" : "Agent khi chạy bộ bench Harbor"}
              <input className="input mono" value={form.agent ?? ""} spellCheck={false}
                placeholder={form.kind === "harbor" ? "oracle, nop, claude-code, codex…" : "để trống = terminus-2"} onChange={(e) => set({ agent: e.target.value })} />
              <span className="hint">{form.kind === "harbor" ? "“oracle” chạy lời giải mẫu: dùng để kiểm tra task có hỏng không." : "Agent điều khiển terminal, gọi model qua Base URL ở trên."}</span>
            </label>
          )}
          {form.kind !== "mock" && (
            <>
              <label className="field">{form.kind === "harbor" ? "Model (provider/model, tuỳ chọn)" : "Tên model gửi lên API"}
                <input className="input mono" value={form.model_name ?? ""} spellCheck={false}
                  placeholder={form.kind === "harbor" ? "vd. anthropic/claude-sonnet-5-5" : "để trống = tên hiển thị"} onChange={(e) => set({ model_name: e.target.value })} />
              </label>
              <label className="field">Tên bí mật chứa API key
                <input className="input mono" value={form.secret_name ?? ""} placeholder="vd. MODEL_X_API_KEY" spellCheck={false} onChange={(e) => set({ secret_name: e.target.value })} />
                <span className="hint">Đặt giá trị trong backend/.env hoặc biến môi trường.</span>
              </label>
            </>
          )}
        </div>
        {error && <p role="alert" className="notice err">{error}</p>}
        <div className="row">
          <button type="button" className="btn btn-primary" disabled={!canAdd} onClick={add}>{adding ? "Đang kiểm tra…" : "Kiểm tra & thêm"}</button>
          <span className="muted" style={{ fontSize: 12 }}>Hệ thống gửi một yêu cầu thử 1 token trước khi lưu. API key không lưu trong trình duyệt.</span>
        </div>
      </section>
    </div>
  );
}
