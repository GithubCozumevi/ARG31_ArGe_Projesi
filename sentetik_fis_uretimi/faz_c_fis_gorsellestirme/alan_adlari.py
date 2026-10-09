"""
data/final_veriler/ içindeki Türkçe JSON + PNG çıktıyı İngilizce alan adlı bir
KOPYAYA dönüştürür. Yol A: üretim koduna (main.py, generators/) hiç dokunmaz,
yalnız var olan final_veriler dosyalarını okuyup data/final_veriler_en/'e yazar
-- "safe yöntem" (2026-08-17 kararı). final_veriler NİHAİ, bu script onu
DEĞİŞTİRMEZ, sadece dışa aktarır.

final_veriler eski şemayı taşıyor (saat yok, is_kolu/harcama_kategorisi hâlâ
girdide) -- bu script hem eski hem yeni şemayı kabul eder: `saat` yoksa
fis_uret.saat_uret ile türetir, is_kolu/harcama_kategorisi girdide bulursa
oradan okur (yoksa etikette arar).

Çıktı: data/final_veriler_en/{bolme}_girdi.json, {bolme}_etiket.json,
{bolme}_gorsel/, ve tek seferlik data/final_veriler_en/business_reference.json
(schema.py'den TÜRETİLİR, elle yazılmaz -- iki kaynak sapmasın diye).

Kullanım:
    python -m faz_c_fis_gorsellestirme.alan_adlari
    python -m faz_c_fis_gorsellestirme.alan_adlari --kaynak-dizin data/deneme/final_veriler \
        --hedef-dizin data/deneme/final_veriler_en   # deneme koşusu için

`$D` KULLANMAYAN tek faz_c_fis_gorsellestirme adımıydı (2026-09-02'de argüman eklendi); artık
diğer adımlar gibi koşu dizinini alır. Varsayılan yollar gerçek NİHAİ set.
"""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from faz_c_fis_gorsellestirme.fis_uret import saat_tohumlari_kur, saat_uret, tarih_tr
from ortak.schema import IS_KOLU_KATEGORILERI, POLICY_YASAKLI_KATEGORILER

VARSAYILAN_KAYNAK_DIZIN = Path("data/final_veriler")
VARSAYILAN_HEDEF_DIZIN = Path("data/final_veriler_en")
BOLMELER = ["egitim", "dogrulama", "test"]

BUSINESS_LINE = {
    "restoran": "restaurant", "market": "grocery_store", "otel": "hotel",
    "ofis_tedarik": "office_supplies_store", "teknoloji": "technology",
    "danismanlik_firmasi": "consulting_firm", "lojistik_firmasi": "logistics_firm",
    "ulasim_saglayici": "transport_provider", "organizasyon": "event_organizer",
    # 2026-08-14'te şemadan kaldırıldı ama final_veriler (24.132, NİHAİ) hâlâ
    # taşıyor (%20,4) -- geriye dönük eşleme burada kalmalı.
    "kisisel_bakim": "personal_care_store", "giyim_magazasi": "clothing_store",
}
EXPENSE_CATEGORY = {
    "yemek_hizmeti": "food_service", "temel_gida": "basic_groceries",
    "ulasim_hizmeti": "transport_service", "ulasim_bireysel": "individual_transport",
    "konaklama": "accommodation", "ofis_sarf_malzeme": "office_supplies",
    "ofis_mobilya": "office_furniture", "teknoloji_ekipman": "tech_equipment",
    "yazilim_lisans": "software_license", "danismanlik": "consulting",
    "alkol": "alcohol", "eglence": "entertainment", "tutun_urunleri": "tobacco",
    "kumar": "gambling", "temizlik": "cleaning_supplies", "kisisel_bakim": "personal_care",
    # ayni gerekce: final_veriler'de hala var.
    "giyim": "clothing", "diger": "other",
}
ANOMALY_TYPE = {
    "gelecek_tarihli": "future_dated", "gecersiz_kimlik_no": "invalid_tax_id",
    "is_kolu_kategori_uyumsuzlugu": "business_category_mismatch",
    "yasakli_kategori": "prohibited_category", "limit_asimi": "limit_exceeded",
    "kdv_tutari": "vat_amount_mismatch", "satir_toplami": "line_total_mismatch",
    "ondalik_kaymasi": "decimal_shift_high", "dusuk_ondalik_kaymasi": "decimal_shift_low",
    "genel_toplam": "total_mismatch", "footer_kismi": "footer_mismatch",
    "mukerrer_fis_yukleme": "duplicate_upload", "fatura_no_cakismasi": "receipt_no_collision",
    # 2026-08-17'de emekliye ayrildi (kalem bazli ara toplam disa aktarimdan
    # kalkinca gorunmez oluyordu) ama final_veriler bu karardan ONCE uretildi.
    "ara_toplam": "line_subtotal_mismatch",
}
NOTE_QUALITY = {"yeterli": "sufficient", "yetersiz": "insufficient",
                 "manipulatif": "manipulative", "ai_uretimi": "ai_generated"}
APPROVAL_STATUS = {"onaylandi": "approved", "onaylanmadi": "rejected",
                    "gozden_gecirilecek": "review_required"}


def opak_id(kayit_id: str) -> str:
    """kayit_id -> sırasız, kararlı opak kimlik. Ordinal sızıntıyı keser
    (bkz. konuşma geçmişi -- record_id sızıntısı, %30 -> %11,5 ölçümü)."""
    return hashlib.sha256(f"masrafai:{kayit_id}".encode()).hexdigest()[:10].upper()


def zaman_damgasi_tr(iso_zaman: str) -> str:
    """'2026-06-02T11:47:00+03:00' -> '02.06.2026 11:47'."""
    tarih, saat = iso_zaman.split("T")
    return f"{tarih_tr(tarih)} {saat[:5]}"


def girdi_donustur(fatura: dict, saat_tohumlari: dict[str, str]) -> dict:
    tohum = str(saat_tohumlari.get(fatura["kayit_id"], fatura["kayit_id"]))
    return {
        "record_id": opak_id(fatura["kayit_id"]),
        "company": fatura["satici_unvan"],
        "address": fatura["adres"],
        "seller_tax_id": fatura["satici_vkn"],
        "receipt_no": fatura["fatura_no"],
        "date": tarih_tr(fatura["fatura_tarihi"]),   # DD.MM.YYYY -- ISO'dan vazgeçildi
        "time": fatura.get("saat") or saat_uret(tohum),
        "items": [
            {
                "item_description": k["aciklama"],
                "quantity": k["miktar"],
                "unit": k["birim"],
                "unit_price": k["birim_fiyat"],
                "vat_rate": k["kdv_orani"],
                "line_total": k["satir_toplam"],
            }
            for k in fatura["kalemler"]
        ],
        "total_vat_amount": fatura["toplam_kdv_tutari"],
        "total_amount": fatura["genel_toplam"],
        "user_note": fatura.get("aciklama_metni", ""),
        "upload_timestamp": zaman_damgasi_tr(fatura["yukleme_zamani"]),
    }


def etiket_donustur(etiket: dict, is_kolu: str, harcama_kategorileri: list[str]) -> dict:
    return {
        "record_id": opak_id(etiket["kayit_id"]),
        "receipt_no": etiket["fatura_no"],
        "duplicate_group_id": etiket.get("cift_grup_id", ""),
        "business_line": BUSINESS_LINE[is_kolu],
        "expense_categories": [EXPENSE_CATEGORY[k] for k in harcama_kategorileri],
        "is_anomaly": etiket["is_anomali"],
        "anomaly_types": [ANOMALY_TYPE[t] for t in etiket["anomali_turleri"]],
        "note_quality": NOTE_QUALITY[etiket["aciklama_kategorisi"]],
        "approval_status": APPROVAL_STATUS[etiket["onay_durumu"]],
    }


def bolme_donustur(bolme: str, kaynak_dizin: Path, hedef_dizin: Path) -> None:
    girdi_yolu = kaynak_dizin / f"{bolme}_girdi.json"
    etiket_yolu = kaynak_dizin / f"{bolme}_etiket.json"
    if not girdi_yolu.exists():
        print(f"[!] {girdi_yolu} yok, atlanıyor.")
        return

    faturalar = json.load(open(girdi_yolu, encoding="utf-8"))
    etiketler = json.load(open(etiket_yolu, encoding="utf-8"))
    saat_tohumlari = saat_tohumlari_kur(faturalar)

    # is_kolu / harcama_kategorileri artık Türkçe etikette (2026-08-17
    # hizalamasından sonra), girdide değil.
    is_kolu_haritasi = {e["kayit_id"]: e["is_kolu"] for e in etiketler}
    harcama_haritasi = {e["kayit_id"]: e["harcama_kategorileri"] for e in etiketler}

    yeni_girdi = [girdi_donustur(f, saat_tohumlari) for f in faturalar]
    yeni_etiket = [
        etiket_donustur(e, is_kolu_haritasi[e["kayit_id"]], harcama_haritasi[e["kayit_id"]])
        for e in etiketler
    ]

    hedef_dizin.mkdir(parents=True, exist_ok=True)
    json.dump(yeni_girdi, open(hedef_dizin / f"{bolme}_girdi.json", "w", encoding="utf-8"),
               ensure_ascii=False, indent=2)
    json.dump(yeni_etiket, open(hedef_dizin / f"{bolme}_etiket.json", "w", encoding="utf-8"),
               ensure_ascii=False, indent=2)

    kaynak_gorsel = kaynak_dizin / f"{bolme}_gorsel"
    if kaynak_gorsel.exists():
        hedef_gorsel = hedef_dizin / f"{bolme}_gorsel"
        hedef_gorsel.mkdir(parents=True, exist_ok=True)
        for f in faturalar:
            kaynak_png = kaynak_gorsel / f"{f['kayit_id']}.png"
            if kaynak_png.exists():
                shutil.copy(kaynak_png, hedef_gorsel / f"{opak_id(f['kayit_id'])}.png")

    print(f"[+] {bolme}: {len(yeni_girdi)} kayıt -> {hedef_dizin}")


def referans_json_yaz(hedef_dizin: Path) -> None:
    """schema.py'deki IS_KOLU_KATEGORILERI + POLICY_YASAKLI_KATEGORILER'dan
    TÜRETİLİR -- elle yazılmaz, iki kaynak asla sapmasın diye."""
    isletme_kategori = {
        BUSINESS_LINE[is_kolu.value]: [EXPENSE_CATEGORY[k.value] for k in kategoriler]
        for is_kolu, kategoriler in IS_KOLU_KATEGORILERI.items()
    }
    yasakli = sorted({EXPENSE_CATEGORY[k.value] for k in POLICY_YASAKLI_KATEGORILER})
    obj = {
        "business_line_expense_categories": isletme_kategori,
        "prohibited_expense_categories": yasakli,
    }
    hedef_dizin.mkdir(parents=True, exist_ok=True)
    hedef = hedef_dizin / "business_reference.json"
    json.dump(obj, open(hedef, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[+] referans -> {hedef}")


def politika_limitleri_yaz(hedef_dizin: Path) -> None:
    """data/politika_limitleri.json'ı İngilizce alan adlarıyla kopyalar --
    limit_asimi anomalisi için model referansı (business_reference.json'un
    kardeşi)."""
    kaynak = json.load(open("data/politika_limitleri.json", encoding="utf-8"))
    kategori_limit = {
        EXPENSE_CATEGORY[k]: v for k, v in kaynak["kategori_limitleri"].items()
    }
    birim_limit = {}
    for anahtar, deger in kaynak["birim_limitleri"].items():
        kategori, birim = anahtar.split("|")
        birim_limit[f"{EXPENSE_CATEGORY[kategori]}|{birim}"] = deger
    obj = {
        "limit_basis": "unit_price",
        "category_limits": kategori_limit,
        "unit_limits": birim_limit,
    }
    hedef = hedef_dizin / "policy_limits.json"
    json.dump(obj, open(hedef, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"[+] politika limitleri -> {hedef}")


def main():
    parser = argparse.ArgumentParser(
        description="Türkçe final_veriler -> İngilizce alan adlı kopya")
    parser.add_argument("--kaynak-dizin", type=Path, default=VARSAYILAN_KAYNAK_DIZIN,
                        help=f"okunacak Türkçe bölme dizini (varsayılan {VARSAYILAN_KAYNAK_DIZIN})")
    parser.add_argument("--hedef-dizin", type=Path, default=VARSAYILAN_HEDEF_DIZIN,
                        help=f"yazılacak İngilizce dizin (varsayılan {VARSAYILAN_HEDEF_DIZIN})")
    args = parser.parse_args()

    for bolme in BOLMELER:
        bolme_donustur(bolme, args.kaynak_dizin, args.hedef_dizin)
    referans_json_yaz(args.hedef_dizin)
    politika_limitleri_yaz(args.hedef_dizin)


if __name__ == "__main__":
    main()
