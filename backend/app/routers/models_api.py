import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ModelEndpoint, RunEntry, Run
from ..services import harbor, llm
from ..services.runner import manager
from ..services.secrets import secrets
from ..timeutil import iso, now

router = APIRouter(prefix="/api/models", tags=["models"])


class ModelIn(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:/-]+$")
    note: str = ""
    role: Literal["contestant", "judge", "both"] = "contestant"
    kind: Literal["openai", "mock", "harbor"] = "openai"
    base_url: str = Field(default="", max_length=500)
    model_name: str | None = None
    secret_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    # Agent Harbor (oracle, nop, terminus-2, claude-code, codex…). Bắt buộc khi kind = harbor.
    agent: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_.:@/-]+$")

    @field_validator("model_name", "secret_name", "agent", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        """Ô để trống = không khai báo. Nếu không có bước này, chuỗi rỗng rơi vào kiểm tra
        pattern và báo lỗi khó hiểu cho một ô người dùng cố tình bỏ trống."""
        return None if isinstance(v, str) and not v.strip() else v


def model_out(m: ModelEndpoint) -> dict:
    return {
        "id": m.id, "note": m.note, "role": m.role, "kind": m.kind, "base_url": m.base_url, "model_name": m.model_name, "agent": m.agent,
        "secret_name": m.secret_name, "secret_present": secrets.has(m.secret_name) if m.secret_name else None,
        "status": m.status, "latency_ms": m.latency_ms, "last_error": m.last_error, "checked_at": iso(m.checked_at),
    }


async def _ping(db: Session, m: ModelEndpoint) -> None:
    if m.kind == "harbor":
        # Agent Harbor không có endpoint để ping: kiểm tra Harbor CLI + môi trường (Docker…) thay thế.
        try:
            await asyncio.to_thread(harbor.preflight)
            m.status, m.latency_ms, m.last_error = "ok", None, None
        except harbor.HarborError as e:
            m.status, m.latency_ms, m.last_error = "down", None, str(e)
        m.checked_at = now()
        db.commit()
        return
    try:
        m.latency_ms = await llm.ping(manager.client, m)
        m.status, m.last_error = "ok", None
    except llm.ModelCallError as e:
        m.status, m.latency_ms, m.last_error = "down", None, str(e)
    m.checked_at = now()
    db.commit()


@router.get("")
def list_models(db: Session = Depends(get_db)):
    return [model_out(m) for m in db.scalars(select(ModelEndpoint).order_by(ModelEndpoint.id))]


@router.post("", status_code=201)
async def create_model(body: ModelIn, db: Session = Depends(get_db)):
    if db.get(ModelEndpoint, body.id):
        raise HTTPException(409, f"Đã có mô hình {body.id}")
    if body.kind == "openai" and not body.base_url.startswith(("http://", "https://")):
        raise HTTPException(422, "Base URL phải bắt đầu bằng http:// hoặc https://")
    if body.kind == "harbor" and not body.agent:
        raise HTTPException(422, "Agent Harbor là bắt buộc (vd. oracle, terminus-2, claude-code)")
    if body.kind == "mock" and not body.base_url:
        body.base_url = "mock://0.7"
    m = ModelEndpoint(**body.model_dump())
    db.add(m)
    db.commit()
    await _ping(db, m)
    return model_out(m)


@router.post("/{model_id}/ping")
async def ping_model(model_id: str, db: Session = Depends(get_db)):
    m = db.get(ModelEndpoint, model_id)
    if m is None:
        raise HTTPException(404, "Không tìm thấy mô hình")
    await _ping(db, m)
    return model_out(m)


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str, db: Session = Depends(get_db)):
    m = db.get(ModelEndpoint, model_id)
    if m is None:
        raise HTTPException(404, "Không tìm thấy mô hình")
    used = db.scalar(select(RunEntry.id).where(RunEntry.model_id == model_id).limit(1)) or db.scalar(select(Run.id).where(Run.judge_id == model_id).limit(1))
    if used:
        raise HTTPException(409, "Mô hình đã có trong lịch sử chạy; không thể xoá.")
    db.delete(m)
    db.commit()
