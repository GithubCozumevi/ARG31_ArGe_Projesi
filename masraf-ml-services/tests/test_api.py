"""
FastAPI uclarinin testleri (MinIO ve MLflow sahte nesnelerle degistirilir; gercek servis gerekmez).
fastapi / httpx / python-multipart kurulu degilse bu testler atlanir (pip install -r requirements-dev.txt).

Calistirma (proje kokunde): python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi.testclient import TestClient
    from minio.error import S3Error

    from app.main import app
    from app.routers import extract as extract_router
    from app.services import storage as storage_service
    from app.services import vendors
    FASTAPI_VAR = True
except ImportError:   # fastapi, httpx, python-multipart ya da minio yok
    FASTAPI_VAR = False

VKN = "1234567890"      # checksum'i gecen sentetik VKN
GIZLI_METIN = "ic-hata-ayrintisi-sizmamali"


if FASTAPI_VAR:
    class NesneYokHatasi(S3Error):
        """S3Error'un kurucu imzasi minio surumleri arasinda degisiyor; testte yalnizca code alani lazim."""

        def __init__(self):   # super().__init__ bilerek cagrilmiyor
            pass

        @property
        def code(self):
            return "NoSuchKey"


class SahteMinio:
    """MinIO istemcisinin kullandigimiz kismi: bellekte {bucket: {nesne_adi: veri}}."""

    def __init__(self):
        self.nesneler = {}
        self.hata = None

    def bucket_exists(self, bucket):
        return bucket in self.nesneler

    def make_bucket(self, bucket):
        self.nesneler[bucket] = {}

    def fput_object(self, bucket, ad, yol):
        if self.hata:
            raise RuntimeError(self.hata)
        with open(yol, "rb") as f:
            self.nesneler[bucket][ad] = f.read()

    def stat_object(self, bucket, ad):
        if ad not in self.nesneler.get(bucket, {}):
            raise NesneYokHatasi()
        return object()

    def list_objects(self, bucket, prefix=None, recursive=False):
        adlar = sorted(a for a in self.nesneler.get(bucket, {}) if not prefix or a.startswith(prefix))
        if not recursive:   # gercek MinIO gibi: alt klasorleri tek "dizin" olarak gosterir
            adlar = sorted({a.split("/")[0] + "/" if "/" in a else a for a in adlar})
        return [mock.Mock(object_name=a) for a in adlar]

    def presigned_get_object(self, bucket, ad, expires=None):
        return f"http://minio.test/{bucket}/{ad}?imza=1"


@unittest.skipUnless(FASTAPI_VAR, "fastapi/httpx/python-multipart/minio kurulu degil")
class ApiTestTabani(unittest.TestCase):
    def setUp(self):
        self.istemci = TestClient(app)
        self.minio = SahteMinio()
        for yama in (
            mock.patch.object(storage_service, "get_client", return_value=self.minio),
            mock.patch.dict(os.environ, {"MLFLOW_LOG_AKTIF": "false", "MINIO_BUCKET": "test-fisler"}),
        ):
            yama.start()
            self.addCleanup(yama.stop)


class SaglikTesti(ApiTestTabani):
    def test_health(self):
        self.assertEqual(self.istemci.get("/health").json(), {"status": "ok"})


class ExtractTesti(ApiTestTabani):
    def test_vkn_cikarir_ve_mlflow_kapaliysa_yazmaz(self):
        yanit = self.istemci.post("/extract/vkn", json={"fis_id": "fis1", "ocr_text": f"VKN: {VKN}"})
        veri = yanit.json()
        self.assertEqual(yanit.status_code, 200)
        self.assertEqual((veri["vkn"], veri["kaynak"], veri["fis_id"]), (VKN, "ocr_regex", "fis1"))
        self.assertEqual(veri["mlflow_log"], "kapali")

    def test_mlflow_aciksa_log_arka_planda_yazilir(self):
        with mock.patch.dict(os.environ, {"MLFLOW_LOG_AKTIF": "true"}), \
             mock.patch.object(extract_router, "_mlflow_logla") as logla:
            veri = self.istemci.post("/extract/vkn", json={"fis_id": "fis2", "ocr_text": f"VKN: {VKN}"}).json()
        self.assertEqual(veri["mlflow_log"], "kuyruga_alindi")
        logla.assert_called_once()
        self.assertEqual(logla.call_args.args[0], "fis2")
        self.assertNotIn("mlflow_log", logla.call_args.args[1])

    def test_mlflow_hatasi_istegi_bozmaz(self):
        with mock.patch.dict(os.environ, {"MLFLOW_LOG_AKTIF": "true"}), \
             mock.patch("app.services.tracking.log_vkn_run", side_effect=RuntimeError(GIZLI_METIN)), \
             self.assertLogs("app.routers.extract", level="WARNING"):
            yanit = self.istemci.post("/extract/vkn", json={"fis_id": "fis3", "ocr_text": f"VKN: {VKN}"})
        self.assertEqual(yanit.status_code, 200)
        self.assertNotIn(GIZLI_METIN, yanit.text)

    def test_sozluk_durum_ve_yenile(self):
        with tempfile.TemporaryDirectory() as klasor:
            yol = os.path.join(klasor, "sozluk.json")
            vendors.sozluk_kaydet(vendors.sozluk_kur([(VKN, "ORNEK MARKET")]), yol)
            with mock.patch.object(extract_router, "sozluk_deposu", vendors.SozlukDeposu(yol)):
                self.assertEqual(self.istemci.get("/extract/sozluk-durum").json()["yuklu_satici_sayisi"], 1)
                vendors.sozluk_kaydet(vendors.sozluk_kur([(VKN, "A"), ("9876543217", "B")]), yol)
                self.assertEqual(self.istemci.post("/extract/sozluk-yenile").json(), {"yuklu_satici_sayisi": 2})
                veri = self.istemci.post(
                    "/extract/vkn", json={"fis_id": "f", "ocr_text": f"VKN: {VKN}", "firma_ipucu": "ORNEK MARKET"}
                ).json()
        self.assertEqual(veri["satici"]["durum"], "bilinen_satici")


class StorageTesti(ApiTestTabani):
    def yukle(self, ad, **parametre):
        return self.istemci.post("/storage/upload", files={"file": (ad, b"gorsel-verisi")}, params=parametre)

    def test_yukleme_bucket_env_den_gelir(self):
        yanit = self.yukle("fis1.jpg")
        self.assertEqual(yanit.status_code, 200)
        self.assertEqual(yanit.json(), {"filename": "fis1.jpg", "bucket": "test-fisler", "status": "uploaded"})
        self.assertEqual(self.minio.nesneler["test-fisler"]["fis1.jpg"], b"gorsel-verisi")

    def test_dosya_adindaki_yol_parcalari_atilir(self):
        self.assertEqual(self.yukle("../../etc/fis2.png").json()["filename"], "fis2.png")
        self.assertEqual(self.yukle("C:\\Users\\x\\fis3.png").json()["filename"], "fis3.png")
        self.assertEqual(sorted(self.minio.nesneler["test-fisler"]), ["fis2.png", "fis3.png"])

    def test_izinsiz_uzanti_reddedilir(self):
        self.assertEqual(self.yukle("calistir.exe").status_code, 400)
        self.assertEqual(self.yukle("uzantisiz").status_code, 400)

    def test_ayni_ad_409_overwrite_ile_ustune_yazar(self):
        self.assertEqual(self.yukle("fis4.jpg").status_code, 200)
        self.assertEqual(self.yukle("fis4.jpg").status_code, 409)
        self.assertEqual(self.yukle("fis4.jpg", overwrite="true").status_code, 200)

    def test_liste_alt_klasorleri_de_gosterir(self):
        self.minio.nesneler["test-fisler"] = {"gercek/fis1.jpg": b"x", "sentetik/fis2.jpg": b"x", "kok.jpg": b"x"}
        self.assertEqual(
            self.istemci.get("/storage/list").json()["files"], ["gercek/fis1.jpg", "kok.jpg", "sentetik/fis2.jpg"]
        )
        self.assertEqual(self.istemci.get("/storage/list", params={"prefix": "gercek/"}).json()["files"], ["gercek/fis1.jpg"])

    def test_indirme_linki_alt_klasorlu_adla_calisir(self):
        self.minio.nesneler["test-fisler"] = {"gercek/fis1.jpg": b"x"}
        yanit = self.istemci.get("/storage/url/gercek/fis1.jpg")
        self.assertEqual(yanit.status_code, 200)
        self.assertEqual(yanit.json()["url"], "http://minio.test/test-fisler/gercek/fis1.jpg?imza=1")

    def test_olmayan_dosya_404(self):
        self.minio.nesneler["test-fisler"] = {}
        self.assertEqual(self.istemci.get("/storage/url/yok.jpg").status_code, 404)

    def test_depo_hatasi_ic_ayrinti_sizdirmaz(self):
        self.minio.hata = GIZLI_METIN
        with self.assertLogs("app.routers.storage", level="ERROR"):
            yanit = self.yukle("fis5.jpg")
        self.assertEqual(yanit.status_code, 502)
        self.assertNotIn(GIZLI_METIN, yanit.text)


class TrackingTesti(ApiTestTabani):
    ISTEK = {"fis_id": "f1", "processing_time_sec": 1.5, "confidence_score": 0.9, "result_json": {"a": 1}}

    def test_basarili_log(self):
        with mock.patch("app.services.tracking.log_ocr_run", return_value="run123") as log:
            yanit = self.istemci.post("/ml/log-ocr-run", json=self.ISTEK)
        self.assertEqual(yanit.json(), {"status": "logged", "run_id": "run123"})
        self.assertEqual(log.call_args.kwargs["model_name"], "paddleocr_vlm_v1")

    def test_mlflow_hatasi_502_ve_ayrinti_yok(self):
        with mock.patch("app.services.tracking.log_ocr_run", side_effect=RuntimeError(GIZLI_METIN)), \
             self.assertLogs("app.routers.tracking", level="ERROR"):
            yanit = self.istemci.post("/ml/log-ocr-run", json=self.ISTEK)
        self.assertEqual(yanit.status_code, 502)
        self.assertNotIn(GIZLI_METIN, yanit.text)


if __name__ == "__main__":
    unittest.main()
