"""
MLflow -> MinIO baglantisini test eden basit script.
Calistirmadan once:
  pip install mlflow boto3
  Asagidaki environment degiskenlerini kendi .env degerlerinle degistir.
"""
import os

# MLflow'a hangi sunucuya baglanacagini soyluyoruz
os.environ["MLFLOW_TRACKING_URI"] = "http://localhost:5000"

# MinIO'ya S3 uyumlu istemci gibi baglanmak icin gereken degiskenler
# (infra/mlflow/.env dosyandaki MINIO_ACCESS_KEY / MINIO_SECRET_KEY ile ayni olmali)
os.environ["MLFLOW_S3_ENDPOINT_URL"] = "http://localhost:9000"
os.environ["AWS_ACCESS_KEY_ID"] = "85LDBH96WXEDFS5DRXFY"
os.environ["AWS_SECRET_ACCESS_KEY"] = "uSpy3MKzeC8plP0afyZHx3gpa1Xh6oAdNFPHqFey"

import mlflow

mlflow.set_experiment("test-baglanti")

with mlflow.start_run(run_name="ilk-test-run"):
    # Basit bir parametre ve metrik logluyoruz
    mlflow.log_param("test_parametresi", "merhaba_minio")
    mlflow.log_metric("test_metrigi", 0.95)

    # Kucuk bir dosya olusturup artifact olarak MinIO'ya gonderiyoruz
    with open("test_dosyasi.txt", "w") as f:
        f.write("Bu dosya MLflow uzerinden MinIO'ya kaydedildi.\n")
    mlflow.log_artifact("test_dosyasi.txt")

    print("Run tamamlandi! MLflow arayuzunde (http://localhost:5000) 'test-baglanti' deneyinde gormelisin.")
    print("Run ID:", mlflow.active_run().info.run_id)