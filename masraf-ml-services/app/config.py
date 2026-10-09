"""
Servisin ortam degiskenleri tek yerde. Degerler her cagrida okunur (testlerde degistirilebilsin diye).
Baglanti bilgileri (MinIO, MLflow) kendi servis modullerinde, ayni .env dosyasindan okunur.
"""
import os


def minio_bucket() -> str:
    """Fis gorsellerinin yuklendigi bucket."""
    return os.getenv("MINIO_BUCKET", "masraf-fisler")


def mlflow_log_aktif() -> bool:
    """False ise /extract/vkn sonuclari MLflow'a yazilmaz."""
    return os.getenv("MLFLOW_LOG_AKTIF", "true").strip().lower() == "true"
