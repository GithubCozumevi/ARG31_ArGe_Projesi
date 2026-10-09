"""
TEK KATEGORİ için hedefli ek batch hazırlar (25k koşusundan sonraki dengeleme).

NEDEN AYRI SCRIPT: `batch_hazirla.py` seçimi `anomali_turleri` KOTASINA göre yapar
(§3). 25k koşusu sonrası tek bir `aciklama_kategorisi` eksik kaldığında o mekanizma
uygun değil: karışık batch üretip içinden ayıklamak hem havuzu boşuna tüketir hem
de kota raporunu anlamsızlaştırır. Burada doğrudan kategoriye göre seçilir.

DEĞİŞMEZ: daha önce SEÇİLMİŞ hiçbir `kayit_id` tekrar seçilmez (`--haric-dizin`
altındaki batch dosyaları okunur). Üretilmiş mi üretilmemiş mi bakılmaz; seçilmiş
olması yeter, aksi halde iki koşu aynı fişe iki açıklama yazar.

ÖLÇÜLDÜ (2026-08-06, 25k koşusu): `yetersiz` hayatta kalma oranı %54,7 (diğer
kategoriler %88-92). Kaybın çoğu yakın kopya (4.895 ham kaydın 2.018'i). Hedef
adedi belirlerken bu oranı hesaba kat: net N temiz için kabaca 2N üret.

⚠️ YAKIN KOPYA TUZAĞI: runner'ın dedup birikimi (`kabul_token_setleri`) süreç
başında BOŞ başlar ve yalnız kendi çıktı dosyasından beslenir. Yeni dizinde koşan
ek üretim, MEVCUT koşudaki metinleri GÖRMEZ. Birleştirmeden önce tüm küme üzerinde
tek seferlik global dedup geçmek ŞART (`yakin_kopya_mi`).

    python -m faz_b_aciklama_uretimi.batch_hazirla_kategori --kategori yetersiz --toplam 3500 \
        --haric-dizin data/aciklama_25k --cikti-dizini data/aciklama_yetersiz_ek
"""

import argparse
import glob
import json
import random
from pathlib import Path


def _secilmis_idler(dizin: str) -> set[str]:
    """`--haric-dizin` altındaki batch dosyalarında geçen tüm kayit_id'ler."""
    idler: set[str] = set()
    for yol in glob.glob(str(Path(dizin) / "batch_*.json")):
        if yol.endswith("_ciktilar.json"):
            continue
        for f in json.loads(Path(yol).read_text(encoding="utf-8")):
            idler.add(f["kayit_id"])
    return idler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kategori", required=True)
    ap.add_argument("--toplam", type=int, default=3500)
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--faturalar", default="data/faturalar.json")
    ap.add_argument("--etiketler", default="data/faturalar_etiketler.json")
    ap.add_argument("--haric-dizin", default="data/aciklama_25k",
                    help="daha önce seçilmiş batch'lerin bulunduğu dizin")
    ap.add_argument("--cikti-dizini", required=True)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    haric = _secilmis_idler(a.haric_dizin)
    etiket = {e["kayit_id"]: e for e in json.loads(Path(a.etiketler).read_text(encoding="utf-8"))}
    faturalar = json.loads(Path(a.faturalar).read_text(encoding="utf-8"))

    havuz = [f for f in faturalar
             if etiket[f["kayit_id"]]["aciklama_kategorisi"] == a.kategori
             and f["kayit_id"] not in haric]
    print(f"'{a.kategori}' havuzu: {len(havuz)} (hariç tutulan önceki seçim: {len(haric)})")
    if len(havuz) < a.toplam:
        print(f"UYARI: havuz hedeften küçük, {len(havuz)} kayıtla devam ediliyor.")

    random.seed(a.seed)
    secilen = random.sample(havuz, min(a.toplam, len(havuz)))

    # Batch kaydı ŞEMASI mevcut batch'lerle BİREBİR aynı olmalı; runner bu alanları
    # okur ve `aciklama_kategorisi` prompt kurulumuna girer.
    def kayit(f: dict) -> dict:
        e = etiket[f["kayit_id"]]
        return {
            "kayit_id": f["kayit_id"],
            "fatura_no": f["fatura_no"],
            "fatura_tarihi": f["fatura_tarihi"],
            "satici_unvan": f["satici_unvan"],
            "kalemler": f["kalemler"],
            "aciklama_kategorisi": e["aciklama_kategorisi"],
            "is_anomali": e["is_anomali"],
            "anomali_turleri": e["anomali_turleri"],
        }

    dizin = Path(a.cikti_dizini)
    dizin.mkdir(parents=True, exist_ok=True)
    batchler = []
    for i in range(0, len(secilen), a.batch_size):
        dilim = secilen[i:i + a.batch_size]
        ad = f"batch_{i // a.batch_size + 1:04d}.json"
        (dizin / ad).write_text(
            json.dumps([kayit(f) for f in dilim], ensure_ascii=False, indent=2),
            encoding="utf-8")
        batchler.append({"dosya": ad,
                         "cikti_dosyasi": ad.replace(".json", "_ciktilar.json"),
                         "adet": len(dilim), "tamam": False})

    (dizin / "durum.json").write_text(json.dumps({
        "config": {"kategori": a.kategori, "toplam": len(secilen),
                   "batch_size": a.batch_size, "seed": a.seed,
                   "haric_dizin": a.haric_dizin},
        "toplam_secilen": len(secilen),
        "batch_sayisi": len(batchler),
        "batchler": batchler,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{len(batchler)} batch yazıldı -> {dizin}")
    for b in batchler:
        print(f"   {b['dosya']}  {b['adet']}")
    print(f"\nKaggle'a YÜKLENECEK: {dizin} dizininin TAMAMI "
          f"(batch_*.json + durum.json)")


if __name__ == "__main__":
    main()
