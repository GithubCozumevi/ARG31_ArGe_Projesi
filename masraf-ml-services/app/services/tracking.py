"""
MLflow ile konusan servis katmani.
OCR/VLM run'larini ve model egitim run'larini loglamak icin kullanilir.

MLflow ilk kullanimda iceri alinir (modul acilisinda degil): servis MLflow kurulu/acik olmadan da ayaga kalkabilir.
Artifact'larin MinIO'ya yazilabilmesi icin .env'de MLFLOW_S3_ENDPOINT_URL, AWS_ACCESS_KEY_ID ve
AWS_SECRET_ACCESS_KEY tanimli olmalidir (mlflow kutuphanesi bunlari kendisi okur).
"""
import os


def _mlflow():
    import mlflow

    mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
    return mlflow


def log_ocr_run(
    experiment_name: str,
    run_name: str,
    model_name: str,
    processing_time_sec: float,
    confidence_score: float,
    result_json: dict,
) -> str:
    """
    Bir OCR/VLM cikarimini MLflow'a "run" olarak loglar.
    Donen deger: olusturulan run'in ID'si.
    """
    mlflow = _mlflow()
    mlflow.set_experiment(experiment_name)

    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_param("model", model_name)
        mlflow.log_metric("processing_time_sec", processing_time_sec)
        mlflow.log_metric("confidence_score", confidence_score)
        mlflow.log_dict(result_json, "ocr_output.json")

        return run.info.run_id


def log_vkn_run(fis_id: str, sonuc: dict) -> str:
    """VKN cikarim sonucunu MLflow'a loglar (kaynak, guven, insan kontrolu gerekip gerekmedigi)."""
    mlflow = _mlflow()
    mlflow.set_experiment("vkn-extraction")

    with mlflow.start_run(run_name=f"vkn-{fis_id}") as run:
        mlflow.log_param("kaynak", sonuc.get("kaynak"))
        mlflow.log_param("guven", sonuc.get("guven"))
        mlflow.log_metric("insan_kontrolu", 1.0 if sonuc.get("insan_kontrolu") else 0.0)
        mlflow.log_metric("vkn_bulundu", 1.0 if sonuc.get("vkn") else 0.0)
        mlflow.log_dict({k: v for k, v in sonuc.items() if k != "vkn"}, "karar_detayi.json")
        return run.info.run_id
