"""
GENEL ARAÇ (tek seferlik DEĞİL -- final_veriler her değiştiğinde tekrar
çalıştırılır, bkz. commit.txt): docs/raporlar/bolme_raporu.html'i
data/final_veriler/bolme_raporu.json'daki (2026-08-18'de tazelenmiş) güncel
sayılarla yeniden üretir. HTML/CSS/JS şablonuna DOKUNMAZ, yalnız içine gömülü
`const D = {...}` bloğunu ve elle yazılmış birkaç düzyazı rakamını değiştirir.

Rapor 2026-08-11'de donmuştu (24.132 kayıt, 14 anomali türü -- ara_toplam
dahil); o tarihten sonra giyim/ara_toplam kayıtları silindi (24.132 -> 21.564),
360 kayıtlık limit_asimi+ondalik birleştirmesi yapıldı, 9 kayıtlık split-sızıntı
düzeltildi. `bolme_raporu.json` bu değişikliklerin hepsinden SONRA (2026-08-18
16:22) yazıldı, dolayısıyla güncel -- script onu okuyup HTML'i buna göre günceller.

`sapma.tur`/`sapma.kat`/`sapma.onay` formülü orijinal üretici script'te
belgelenmemiş; buradaki tanım ("bölmeler arası en büyük fark") başlıktaki
etiketle birebir örtüşüyor: her tür/kategori için bölmeler arası oran
farkının (max-min) en büyüğü.

Kullanım (repo kökünden):
    python dataset_quality_reports/final_veriler_bolme_raporu_html_uret.py
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faz_c_fis_gorsellestirme.veri_bol import gruplari_kur
from faz_b_aciklama_uretimi.aciklama_uretim_core import _dedup_normalize

RAPOR_JSON = Path("data/final_veriler/bolme_raporu.json")
HTML_YOLU = Path("docs/raporlar/bolme_raporu.html")
FINAL_DIZIN = Path("data/final_veriler")
BOLMELER = ["egitim", "dogrulama", "test"]
KATS = ["yeterli", "yetersiz", "manipulatif", "ai_uretimi"]
ONAYLAR = ["onaylandi", "gozden_gecirilecek", "onaylanmadi"]


def en_buyuk_grup_detayi() -> tuple[int, int]:
    """(en büyük grup boyutu, o grup içindeki farklı ham metin sayısı)."""
    girdi_all, girdi_by_id = [], {}
    for b in BOLMELER:
        g = json.loads((FINAL_DIZIN / f"{b}_girdi.json").read_text(encoding="utf-8"))
        girdi_all.extend(g)
        for r in g:
            girdi_by_id[r["kayit_id"]] = r
    gruplar = gruplari_kur(girdi_all)
    en_buyuk_id = max(gruplar, key=lambda g: len(gruplar[g]))
    uyeler = gruplar[en_buyuk_id]
    metinler = {_dedup_normalize(girdi_by_id[k]["aciklama_metni"]) for k in uyeler}
    return len(uyeler), len(metinler)


def max_fark(rapor: dict, eksen: str, anahtarlar: list[str], oran_alani: str) -> float:
    en_buyuk = 0.0
    for anahtar in anahtarlar:
        oranlar = [rapor["bolmeler"][b][eksen].get(anahtar, {}).get(oran_alani, 0.0) for b in BOLMELER]
        en_buyuk = max(en_buyuk, max(oranlar) - min(oranlar))
    return round(en_buyuk, 4)


def main() -> None:
    rapor = json.loads(RAPOR_JSON.read_text(encoding="utf-8"))

    turler = sorted(
        rapor["bolmeler"]["egitim"]["anomali_turleri"].keys(),
        key=lambda t: -sum(rapor["bolmeler"][b]["anomali_turleri"][t]["adet"] for b in BOLMELER),
    )
    # turun_orani, TÜRÜN kendi toplamına göre bölmelere dağılımıdır (70/15/15
    # PAYLAŞIMI), kat/onay'daki gibi bölmeler arası doğrudan karşılaştırma DEĞİL
    # -- sapma, hedef bölme oranından (hedef_oranlar) ne kadar saptığıdır.
    hedef_oranlar = rapor["hedef_oranlar"]
    sapma_tur = max(
        abs(rapor["bolmeler"][b]["anomali_turleri"][t]["turun_orani"] - hedef_oranlar[b])
        for t in turler for b in BOLMELER
    )
    sapma_kat = max_fark(rapor, "aciklama_kategorisi", KATS, "oran")
    sapma_onay = max_fark(rapor, "onay_durumu", ONAYLAR, "oran")

    D = {
        "rapor": rapor,
        "bolmeler": BOLMELER,
        "turler": turler,
        "kats": KATS,
        "onaylar": ONAYLAR,
        "sapma": {"tur": round(sapma_tur, 4), "kat": sapma_kat, "onay": sapma_onay},
    }

    en_buyuk_boyut, en_buyuk_metin_sayisi = en_buyuk_grup_detayi()
    assert en_buyuk_boyut == rapor["dogrulama"]["en_buyuk_grup"]

    html = HTML_YOLU.read_text(encoding="utf-8")

    html = re.sub(r"const D = \{.*\};", "const D = " + json.dumps(D, ensure_ascii=False) + ";", html)
    html = html.replace(
        "24.132 kayıtlık veri setinin eğitim, doğrulama ve test olarak ayrılması.",
        f"{rapor['toplam_kayit']:,}".replace(",", ".") + " kayıtlık veri setinin eğitim, doğrulama ve test olarak ayrılması.",
    )
    html = re.sub(
        r"En büyük grup bu yüzden \d+ kayda ulaşıyor\s*\(\d+ farklı ham metin, çoğu kısa <em>yetersiz</em> notu\)\.",
        f"En büyük grup bu yüzden {en_buyuk_boyut} kayda ulaşıyor\n  ({en_buyuk_metin_sayisi} farklı ham metin, "
        f"çoğu kısa <em>yetersiz</em> notu).",
        html,
    )
    html = re.sub(r"Bu \d+ kayıt bölünemez,", f"Bu {en_buyuk_boyut} kayıt bölünemez,", html)
    html = re.sub(
        r"<footer>veri_bol\.py.*?</footer>",
        "<footer>veri_bol.py &middot; gruplu bölme, union-find &middot; 2026-08-19 (final_veriler_bolme_raporu_html_uret.py ile yenilendi)</footer>",
        html,
    )

    HTML_YOLU.write_text(html, encoding="utf-8")
    print(f"[+] {HTML_YOLU} güncellendi -- {rapor['toplam_kayit']} kayıt, "
          f"{len(turler)} tür, en büyük grup {en_buyuk_boyut} ({en_buyuk_metin_sayisi} ham metin)")


if __name__ == "__main__":
    main()
