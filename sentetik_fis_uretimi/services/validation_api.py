"""Tek fiş anomali tespit servisi.

Kapsam BİLİNÇLİ olarak TEK faturayla sınırlı (bkz. CLAUDE.md madde 8):
`kural_ihlali_turlerini_tespit_et` zaten çift gerektiren türlere (mukerrer_fis_yukleme,
fatura_no_cakismasi) bakmaz. Bu servis o iki türü, girdi olarak opsiyonel bir
"es_fatura" (aynı klasörden gelen eş kayıt) verildiğinde AYRICA, `cift_grup.py`'nin
zaten kullandığı imza karşılaştırmasıyla ekler -- yeniden icat etmez.

Tarih ("gelecek_tarihli") notu: `kural_ihlali_turlerini_tespit_et` bunu kendi içinde
`date.today()` ile hesaplar; main.py'de ÜRETİM ANINDA çağrıldığı için orada doğrudur.
Burada fatura üretimden çok sonra, gerçek dünya saatinden bağımsız olarak
doğrulanıyor -- doğru referans fişin YÜKLEME ANI'dır (schema.py: "yukleme <
fatura tarihi çıkar, anomalinin tanımı zaten budur"). Bu yüzden validators.py'ye
DOKUNULMADAN (main.py'yi etkilemesin diye), fonksiyonun kendi hesapladığı
gelecek_tarihli burada atılıp `yukleme_zamani` referansıyla yeniden hesaplanır.

Matematiksel tutarlılık (satir_toplami/genel_toplam/footer_kismi) notu -- KRİTİK:
`FaturaKalemi.satir_toplam` (ve ara_toplam/kdv_tutari) schema.py'de STORED alan
DEĞİL, her zaman miktar*birim_fiyat*kdv_orani'ndan YENİDEN HESAPLANAN bir
`@property`. Anomali üretici (`AnomaliliFaturaKalemi`) bunu `sahte_satir_toplam`
gibi override alanlarla bozuyor, ama bu override JSON'a hiç YAZILMIYOR -- yalnız
o anki (bozuk) SAYI export ediliyor. Yani JSON'dan `Fatura(**...)` ile nesne
YENİDEN KURULUNCA, property kendini miktar/birim_fiyat'tan taze hesaplar ve
enjekte edilmiş bozukluğu SESSİZCE YOK EDER -- `kural_ihlali_turlerini_tespit_et`
bu tipi (satir_toplami/genel_toplam/footer_kismi) reconstruct edilmiş nesnede
ASLA yakalayamaz (ölçüldü: K0001159, etikette var, fonksiyonda hep yok çıkıyordu).
Çözüm: bu üç tür, Pydantic nesnesinin property'sinden DEĞİL, POST edilen HAM
JSON sayılarından (`final_veriler_matematik_dogrula.py` ile aynı formül)
ayrıca kontrol edilir -- bkz. `_matematik_ihlalleri`.
"""

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from ortak.schema import Fatura
from ortak.validators import fatura_dogrula, kural_ihlali_turlerini_tespit_et, tarih_gelecekte_mi
from faz_a_fis_uretimi.main import fatura_to_dict
from ortak.cift_grup import cift_grup_anahtari, kayit_imzasi

app = FastAPI(title="Validation API", description="Fiş anomali tespit servisi", version="1.0.0")

TOLERANS = Decimal("0.01")
# Reconstruct edilmiş Pydantic nesnesinde ASLA tetiklenmeyen türler (yukarıdaki
# docstring notu) -- kural_ihlali_turlerini_tespit_et'ten gelirlerse atılıp ham
# JSON kontrolüyle yeniden hesaplanır.
_HAM_JSON_GEREKTIREN_TURLER = {"satir_toplami", "genel_toplam", "footer_kismi", "kdv_tutari"}


def _D(x) -> Decimal:
    return Decimal(str(x))


def _yuvarla(deger: Decimal) -> Decimal:
    return deger.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _vergisiz_hesapla(fatura_ham: dict) -> Decimal:
    """Σ(miktar*birim_fiyat) -- iskonto her zaman 0 (özellik kalktı), bu yüzden
    ara_toplam == miktar*birim_fiyat. `sahte_toplam_vergisiz_tutar` hiçbir zaman
    JSON'a export edilmediği (2026-08-17) için bu değer HER ZAMAN doğrudan
    stored alanlardan (birim_fiyat/miktar) güvenle yeniden hesaplanabilir --
    footer_kismi/genel_toplam'ın aksine burada "ham sayıyı kaybetme" riski yok."""
    return _yuvarla(sum(
        (_yuvarla(_D(k["birim_fiyat"]) * _D(k["miktar"])) for k in fatura_ham["kalemler"]),
        Decimal("0"),
    ))


def _matematik_ihlalleri(fatura_ham: dict) -> set[str]:
    """`satir_toplami`/`genel_toplam`/`footer_kismi`'yi POST edilen ham sayılardan
    tespit eder (final_veriler_matematik_dogrula.py ile AYNI formül, iskonto
    her zaman 0 kabul edilir -- özellik zaten kalktı).

    footer_kismi'nin GERÇEK tanımı (validators.fatura_footer_tutarlilik_dogrula):
    genel_toplam == toplam_vergisiz_tutar + toplam_kdv_tutari. Bu TEK formül,
    kaynağı ne olursa olsun (kdv_tutari doğrudan bozulmuş OLSUN ya da genel_toplam
    bozulmasının matematiksel yan etkisi OLSUN -- CLAUDE.md: "genel_toplam ⊂
    footer_kismi, injector yan etkisi, kaçınılmaz") ikisini de yakalar."""
    turler: set[str] = set()
    for k in fatura_ham["kalemler"]:
        beklenen = _yuvarla(_D(k["birim_fiyat"]) * _D(k["miktar"]) * (1 + _D(k["kdv_orani"]) / 100))
        if abs(_D(k["satir_toplam"]) - beklenen) > TOLERANS:
            turler.add("satir_toplami")

    genel_bekl = _yuvarla(sum((_D(k["satir_toplam"]) for k in fatura_ham["kalemler"]), Decimal("0")))
    if abs(_D(fatura_ham["genel_toplam"]) - genel_bekl) > TOLERANS:
        turler.add("genel_toplam")

    footer_bekl = _yuvarla(_vergisiz_hesapla(fatura_ham) + _D(fatura_ham["toplam_kdv_tutari"]))
    if abs(_D(fatura_ham["genel_toplam"]) - footer_bekl) > TOLERANS:
        turler.add("footer_kismi")

    return turler


def _matematik_ihlal_detaylari(fatura_ham: dict) -> list[str]:
    """`_matematik_ihlalleri` ile aynı hesaplar, insan-okur mesaj üretir --
    `fatura_dogrula`'nın (property-tabanlı) bu türler için ASLA üretemediği
    mesajların ham-JSON karşılığı."""
    detaylar: list[str] = []
    for k in fatura_ham["kalemler"]:
        beklenen = _yuvarla(_D(k["birim_fiyat"]) * _D(k["miktar"]) * (1 + _D(k["kdv_orani"]) / 100))
        if abs(_D(k["satir_toplam"]) - beklenen) > TOLERANS:
            detaylar.append(
                f"Kalem {k['kalem_no']}: satir_toplam hesabı hatalı "
                f"({k['satir_toplam']} beklenen {beklenen})"
            )

    genel_bekl = _yuvarla(sum((_D(k["satir_toplam"]) for k in fatura_ham["kalemler"]), Decimal("0")))
    if abs(_D(fatura_ham["genel_toplam"]) - genel_bekl) > TOLERANS:
        detaylar.append(
            f"Genel toplam, kalemlerin toplamıyla uyuşmuyor "
            f"({fatura_ham['genel_toplam']} beklenen {genel_bekl})"
        )

    footer_bekl = _yuvarla(_vergisiz_hesapla(fatura_ham) + _D(fatura_ham["toplam_kdv_tutari"]))
    if abs(_D(fatura_ham["genel_toplam"]) - footer_bekl) > TOLERANS:
        detaylar.append(
            f"Footer tutarlılığı bozuk: genel_toplam, (vergisiz + KDV) toplamıyla uyuşmuyor "
            f"({fatura_ham['genel_toplam']} beklenen {footer_bekl})"
        )

    return detaylar


class ValidateRequest(BaseModel):
    fatura: Fatura
    es_fatura: Optional[Fatura] = None


class ValidateResponse(BaseModel):
    is_anomaly: bool
    anomaly_types: list[str]
    violation_details: list[str]
    ground_truth: Optional[dict] = None


def _yukleme_tarihi(fatura: Fatura) -> str:
    """Referans tarih: fişin sisteme yüklendiği anın TARİH kısmı (ISO YYYY-MM-DD).

    `saat` (fiş üzerinde basılı, yalnız HH:MM) kasıtlı olarak katılmaz: kendi
    tarihi yok, `yukleme_zamani`'nın gün hassasiyeti anomalinin tanımıyla zaten
    birebir örtüşüyor (bkz. modül docstring'i)."""
    if fatura.yukleme_zamani:
        return fatura.yukleme_zamani[:10]
    return fatura.fatura_tarihi


def _tek_fatura_turleri(fatura: Fatura, fatura_ham: dict) -> set[str]:
    turler = kural_ihlali_turlerini_tespit_et(fatura)
    # satir_toplami/genel_toplam/footer_kismi/kdv_tutari reconstruct edilmiş
    # nesnenin property'sinden ASLA gelmez (modül docstring'i) -- atılıp ham
    # JSON'dan yeniden hesaplanır. kdv_tutari'nin kendi ham karşılığı yok
    # (kalem.kdv_tutari artık export edilmiyor); sinyali zaten satir_toplami
    # üzerinden yakalanır (sahte kdv_tutari satir_toplam'ı da kaydırır).
    turler -= _HAM_JSON_GEREKTIREN_TURLER
    turler |= _matematik_ihlalleri(fatura_ham)

    turler.discard("gelecek_tarihli")
    if tarih_gelecekte_mi(fatura.fatura_tarihi, _yukleme_tarihi(fatura)):
        turler.add("gelecek_tarihli")
    return turler


def _cift_turu(fatura: Fatura, es_fatura: Fatura) -> Optional[str]:
    """Aynı (satici_vkn, fatura_no) anahtarına sahip iki kayıt mükerrer yükleme mi
    (fişe basılan her şey birebir aynı) yoksa fatura_no çakışması mı (yalnızca
    kimlik alanları ortak, kalemler farklı)? Ayrım `cift_grup.py`'nin imza
    mekanizmasıyla yapılır -- üretici tarafın (`anomaly_injector`) kullandığı
    ayrımın aynısı."""
    d1, d2 = fatura_to_dict(fatura), fatura_to_dict(es_fatura)
    if cift_grup_anahtari(d1) != cift_grup_anahtari(d2):
        return None
    return "mukerrer_fis_yukleme" if kayit_imzasi(d1) == kayit_imzasi(d2) else "fatura_no_cakismasi"


def _ground_truth(fatura: Fatura) -> dict:
    return {
        "is_anomaly": fatura.is_anomali,
        "anomaly_types": fatura.anomali_turleri,
        "note_quality": fatura.aciklama_kategorisi,
    }


@app.post("/validate", response_model=ValidateResponse)
async def validate(request: Request) -> ValidateResponse:
    body = await request.json()
    try:
        req = ValidateRequest(**body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    fatura = req.fatura
    turler = _tek_fatura_turleri(fatura, body["fatura"])

    if req.es_fatura is not None:
        cift_turu = _cift_turu(fatura, req.es_fatura)
        if cift_turu:
            turler.add(cift_turu)

    bugun_str = _yukleme_tarihi(fatura)
    detaylar = fatura_dogrula(fatura, bugun_str) + _matematik_ihlal_detaylari(body["fatura"])

    return ValidateResponse(
        is_anomaly=bool(turler),
        anomaly_types=sorted(turler),
        violation_details=detaylar,
        ground_truth=_ground_truth(fatura),
    )


STATIC_DIR = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
