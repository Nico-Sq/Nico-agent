"""Material and claim HTTP routes and request models."""

from typing import Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from nico_application import MaterialUseCases

from .backend import active_backend, active_store


router = APIRouter()


def active_material_use_cases():
    return MaterialUseCases(active_store())


class MaterialInput(BaseModel):
    kind: Literal["resume", "job_description", "project"]
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)


class ClaimInput(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    material_id: str | None = None


class MaterialEdit(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)


class ClaimEdit(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@router.get("/api/materials")
def list_materials():
    return active_material_use_cases().list_materials()


@router.post("/api/materials")
def create_material(body: MaterialInput):
    try:
        return active_material_use_cases().create_material(body.kind, body.title, body.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/materials/extract")
async def extract_material(file: UploadFile = File(...)):
    data = await file.read(active_backend().max_file_bytes + 1)
    try:
        return {"filename": file.filename, "content": active_backend().extract_text(file.filename or "", data)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/materials/{material_id}")
def edit_material(material_id: str, body: MaterialEdit):
    try:
        material = active_material_use_cases().edit_material(
            material_id, body.title, body.content,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if material is None:
        raise HTTPException(404, "材料不存在或已撤回")
    return material


@router.delete("/api/materials/{material_id}")
def delete_material(material_id: str):
    if not active_material_use_cases().delete_material(material_id):
        raise HTTPException(404, "材料不存在或已撤回")
    return {"revoked": True}


@router.get("/api/claims")
def list_claims():
    return active_material_use_cases().list_claims()


@router.post("/api/claims")
def create_claim(body: ClaimInput):
    try:
        return active_material_use_cases().create_claim(body.text, body.material_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/claims/{claim_id}")
def edit_claim(claim_id: str, body: ClaimEdit):
    try:
        claim = active_material_use_cases().edit_claim(claim_id, body.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if claim is None:
        raise HTTPException(404, "经历不存在或已撤回")
    return claim


@router.delete("/api/claims/{claim_id}")
def delete_claim(claim_id: str):
    if not active_material_use_cases().delete_claim(claim_id):
        raise HTTPException(404, "经历不存在或已撤回")
    return {"revoked": True}


@router.post("/api/claims/{claim_id}/confirm")
def confirm_claim(claim_id: str):
    claim = active_material_use_cases().confirm_claim(claim_id)
    if claim is None:
        raise HTTPException(404, "待确认经历不存在")
    return claim
