#!/usr/bin/env python3
"""
batch_analiz.py -- VAR OLAN batch dosyalarının kota raporunu yeniden basar.

`batch_hazirla.py` seçim bittiğinde tür bazlı kota raporunu terminale basar ve
aynı raporu `durum.json`'a (`kota_raporu`) da yazar. Bu modül, yeni bir seçim
YAPMADAN aynı raporu tekrar görmek içindir: `batch_*.json` dosyalarını okuyup
metrikleri DOSYALARDAKİ kayıtlardan yeniden sayar ve çıktıyı
`batch_hazirla._kota_raporu_yazdir` ile basar -- format kopyalanmadığı için
batch_hazirla'daki rapor biçimi değişirse burası da kendiliğinden uyar.

Neden durum.json'daki hazır raporu okumak yetmiyor: o rapor SEÇİM ANININ
fotoğrafı. Dosyalar sonradan elle düzenlenmiş, bir kısmı silinmiş ya da
`--batch` ile alt küme incelenmek istenmişse gerçeği yalnız dosyaları saymak
verir. durum.json yine de okunur ama yalnız SEÇİM PARAMETRELERİ (tur_taban,
tur_tavan, temiz oranları, kategori hedef oranları) için -- bunlar kayıtlardan
türetilemez, CLI argümanıydı.

Dosyalardan türetilemeyen iki alan:
  - `mevcut_havuzda` (türün TÜM veri setindeki havuz boyutu) -> --etiket-json
    okunarak hesaplanır; dosya verilmez/bulunmazsa durum.json'daki rapordan
    alınır, o da yoksa 0 basılır (sıralama bozulur, sayımlar bozulmaz).
  - `anomalili_kirpildi` bayrağı -> durum.json'dan; yoksa False.

Kullanım:
    python -m faz_b_aciklama_uretimi.batch_analiz --cikti-dizini data/aciklama_25k
    python -m faz_b_aciklama_uretimi.batch_analiz --cikti-dizini data/aciklama_25k --batch 1-10
    python -m faz_b_aciklama_uretimi.batch_analiz --cikti-dizini data/aciklama_25k --etiket-json ""  # havuzsuz, hızlı
"""

import argparse
import json
from collections import Counter
from pathlib import Path

from faz_b_aciklama_uretimi.batch_hazirla import (
    VARSAYILAN_CIKTI_DIZINI,
    VARSAYILAN_KATEGORI_HEDEF_ORANLARI,
    _konteyner_tavanlarini_hesapla,
    _kota_raporu_yazdir,
)


def batch_dosyalarini_yukle(cikti_dizini: Path, aralik: tuple[int, int] | None = None) -> tuple[list[dict], list[str]]:
    """batch_NNNN.json dosyalarını sırayla okur. `_ciktilar.json` DEĞİL --
    onlar runner'ın ürettiği açıklamalar; kota raporu girdi kümesine bakar."""
    dosyalar = sorted(p for p in cikti_dizini.glob("batch_*.json") if not p.name.endswith("_ciktilar.json"))
    if aralik is not None:
        bas, son = aralik
        dosyalar = [p for p in dosyalar if bas <= int(p.stem.split("_")[1]) <= son]

    kayitlar: list[dict] = []
    okunanlar: list[str] = []
    for dosya in dosyalar:
        with open(dosya, "r", encoding="utf-8") as f:
            kayitlar.extend(json.load(f))
        okunanlar.append(dosya.name)
    return kayitlar, okunanlar


def havuz_boyutlarini_hesapla(etiket_json: Path) -> dict[str, list[str]]:
    """Tür havuzları: TÜM veri setinde her anomali türüne sahip kayıtlar
    (anomali_turleri union'ı). `anomali_turu_kotali_sec`'teki kurulumun aynısı."""
    with open(etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    tur_havuzlari: dict[str, list[str]] = {}
    for etiket in etiketler:
        if not etiket.get("is_anomali"):
            continue
        for tur in etiket["anomali_turleri"]:
            tur_havuzlari.setdefault(tur, []).append(etiket["kayit_id"])
    return tur_havuzlari


def rapor_yeniden_kur(
    kayitlar: list[dict],
    tur_havuzlari: dict[str, list[str]] | None,
    onceki_rapor: dict | None,
    tur_taban: int,
    tur_tavan: int,
    temiz_orani_min: float,
    temiz_orani_max: float,
    hedef_kategori_oranlari: dict[str, float],
) -> dict:
    """`anomali_turu_kotali_sec`'in döndürdüğü rapor sözlüğünün aynısını,
    seçim yapmadan, elimizdeki batch kayıtlarından kurar."""
    toplam = len(kayitlar)
    anomalili = [k for k in kayitlar if k["is_anomali"]]
    temiz_sayisi = toplam - len(anomalili)
    anomali_orani = len(anomalili) / toplam if toplam else 0.0
    hedef_araligi = (round(1 - temiz_orani_max, 4), round(1 - temiz_orani_min, 4))

    onceki_tur = (onceki_rapor or {}).get("tur_bazli", {})

    # Efektif tavan: havuzlar elimizdeyse konteyner tespiti veriden yeniden
    # yapılır (seçim anındaki hesabın birebir aynısı); değilse durum.json'daki
    # rapordan alınır, o da yoksa çıplak tur_tavan.
    if tur_havuzlari:
        tavan_efektif = _konteyner_tavanlarini_hesapla(tur_havuzlari, tur_taban, tur_tavan)
    else:
        tavan_efektif = {tur: bilgi.get("tavan", tur_tavan) for tur, bilgi in onceki_tur.items()}

    secilen_sayaci: Counter = Counter()
    for kayit in anomalili:
        secilen_sayaci.update(set(kayit["anomali_turleri"]))

    # Türler: havuzdaki TÜM türler (batch'te hiç seçilmemiş tür de raporda
    # "secilen=0" ile görünmeli). Havuz yoksa batch'te görülenlerle yetinilir.
    turler = sorted(tur_havuzlari) if tur_havuzlari else sorted(set(secilen_sayaci) | set(onceki_tur))

    tur_raporu: dict[str, dict] = {}
    hedefin_altinda_kalanlar: list[str] = []
    for tur in turler:
        if tur_havuzlari is not None and tur in tur_havuzlari:
            havuz_boyutu = len(tur_havuzlari[tur])
        else:
            havuz_boyutu = onceki_tur.get(tur, {}).get("mevcut_havuzda", 0)
        secilen = secilen_sayaci.get(tur, 0)
        yetersiz = secilen < tur_taban
        if yetersiz:
            hedefin_altinda_kalanlar.append(tur)
        tavan = tavan_efektif.get(tur, tur_tavan)
        tur_raporu[tur] = {
            "mevcut_havuzda": havuz_boyutu,
            "secilen": secilen,
            "taban": tur_taban,
            "tavan": tavan,
            "konteyner": tavan > tur_tavan,
            "hedefin_altinda": yetersiz,
        }

    kategori_sayaci = Counter(k["aciklama_kategorisi"] for k in kayitlar)
    kategori_raporu = {
        kategori: {"adet": adet, "oran": round(adet / toplam, 4) if toplam else 0.0}
        for kategori, adet in kategori_sayaci.items()
    }

    return {
        "toplam_secilen": toplam,
        "anomalili_sayisi": len(anomalili),
        # Seçim anına ait bayrak; kayıtlardan geri okunamaz.
        "anomalili_kirpildi": (onceki_rapor or {}).get("anomalili_kirpildi", False),
        "temiz_sayisi": temiz_sayisi,
        "anomali_orani": anomali_orani,
        "hedef_anomali_orani_araligi": hedef_araligi,
        "aciklama_kategorisi_hedef_oranlari": hedef_kategori_oranlari,
        "aciklama_kategorisi_override_sayisi": (onceki_rapor or {}).get("aciklama_kategorisi_override_sayisi", 0),
        "anomali_orani_bandin_disinda": not (hedef_araligi[0] <= anomali_orani <= hedef_araligi[1]),
        "tur_bazli": tur_raporu,
        "hedefin_altinda_kalan_turler": hedefin_altinda_kalanlar,
        "aciklama_kategorisi_dagilimi": kategori_raporu,
    }


def _batch_araligi_ayristir(ifade: str) -> tuple[int, int]:
    if "-" in ifade:
        bas, son = ifade.split("-", 1)
        return int(bas), int(son)
    tek = int(ifade)
    return tek, tek


def main():
    parser = argparse.ArgumentParser(
        description="Var olan batch dosyalarının kota raporunu (batch_hazirla formatında) yeniden bas."
    )
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI,
                        help=f"batch_*.json + durum.json dizini (varsayilan: {VARSAYILAN_CIKTI_DIZINI})")
    parser.add_argument("--etiket-json", default="data/faturalar_etiketler.json",
                        help="Tür havuzu boyutlarını (mevcut_havuzda) hesaplamak için. Boş string verilirse okunmaz.")
    parser.add_argument("--batch", default=None,
                        help="Yalnız bu batch aralığını raporla, ör. 1-10 veya 7 (varsayilan: hepsi)")
    parser.add_argument("--tur-taban", type=int, default=None, help="durum.json'daki degeri ezer")
    parser.add_argument("--tur-tavan", type=int, default=None, help="durum.json'daki degeri ezer")
    parser.add_argument("--json-cikti", default=None, help="Rapor sözlüğünü bu dosyaya da JSON olarak yaz")
    args = parser.parse_args()

    cikti_dizini = Path(args.cikti_dizini)
    if not cikti_dizini.is_dir():
        print(f"HATA: dizin yok -> {cikti_dizini}")
        return

    durum_yolu = cikti_dizini / "durum.json"
    durum: dict = {}
    if durum_yolu.exists():
        with open(durum_yolu, "r", encoding="utf-8") as f:
            durum = json.load(f)
    else:
        print(f"[!] {durum_yolu} yok -- secim parametreleri varsayilana dusuyor (taban/tavan 300/600).")

    config = durum.get("config", {})
    onceki_rapor = durum.get("kota_raporu")
    tur_taban = args.tur_taban if args.tur_taban is not None else config.get("tur_taban", 300)
    tur_tavan = args.tur_tavan if args.tur_tavan is not None else config.get("tur_tavan", 600)
    temiz_min = config.get("temiz_orani_min", 0.70)
    temiz_max = config.get("temiz_orani_max", 0.75)
    hedef_kategori_oranlari = {
        "yeterli": config.get("kategori_oran_yeterli", VARSAYILAN_KATEGORI_HEDEF_ORANLARI["yeterli"]),
        "yetersiz": config.get("kategori_oran_yetersiz", VARSAYILAN_KATEGORI_HEDEF_ORANLARI["yetersiz"]),
        "manipulatif": config.get("kategori_oran_manipulatif", VARSAYILAN_KATEGORI_HEDEF_ORANLARI["manipulatif"]),
        "ai_uretimi": config.get("kategori_oran_ai_uretimi", VARSAYILAN_KATEGORI_HEDEF_ORANLARI["ai_uretimi"]),
    }

    aralik = _batch_araligi_ayristir(args.batch) if args.batch else None
    kayitlar, okunanlar = batch_dosyalarini_yukle(cikti_dizini, aralik)
    if not kayitlar:
        print(f"HATA: {cikti_dizini} icinde batch_*.json bulunamadi (aralik: {args.batch or 'hepsi'}).")
        return
    print(f"[+] {len(okunanlar)} batch dosyasi okundu ({okunanlar[0]} ... {okunanlar[-1]}), "
          f"{len(kayitlar)} kayit -> {cikti_dizini}/")

    tur_havuzlari = None
    if args.etiket_json:
        etiket_yolu = Path(args.etiket_json)
        if etiket_yolu.exists():
            print(f"[+] {etiket_yolu} okunuyor (havuz boyutlari icin, buyuk dosya)...")
            tur_havuzlari = havuz_boyutlarini_hesapla(etiket_yolu)
        else:
            print(f"[!] {etiket_yolu} yok -- havuz boyutlari durum.json'daki rapordan alinacak.")

    rapor = rapor_yeniden_kur(
        kayitlar, tur_havuzlari, onceki_rapor,
        tur_taban=tur_taban, tur_tavan=tur_tavan,
        temiz_orani_min=temiz_min, temiz_orani_max=temiz_max,
        hedef_kategori_oranlari=hedef_kategori_oranlari,
    )
    _kota_raporu_yazdir(rapor)

    if args.json_cikti:
        with open(args.json_cikti, "w", encoding="utf-8") as f:
            json.dump(rapor, f, ensure_ascii=False, indent=2)
        print(f"\n[+] Rapor JSON olarak yazildi -> {args.json_cikti}")


if __name__ == "__main__":
    main()
