from dotenv import load_dotenv
load_dotenv()  # .env dosyasindaki degiskenleri (MINIO_ACCESS_KEY vb.) yukler

from fastapi import FastAPI
from app.routers import storage, tracking, extract

app = FastAPI(
    title="Masraf ML Service",
    description="OCR, NLP ve anomali tespiti icin ML servisi",
    version="0.1.0",
)

app.include_router(storage.router, prefix="/storage", tags=["storage"])
app.include_router(tracking.router, prefix="/ml", tags=["tracking"])
app.include_router(extract.router, prefix="/extract", tags=["extract"])


@app.get("/health")
def health_check():
    """Servisin ayakta olup olmadigini kontrol etmek icin basit bir endpoint."""
    return {"status": "ok"}