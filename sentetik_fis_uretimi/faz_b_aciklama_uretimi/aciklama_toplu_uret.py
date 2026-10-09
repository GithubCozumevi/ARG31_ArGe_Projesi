"""
Toplu açıklama üretim runner'ı: batch_hazirla.py'nin ürettiği batch
dosyalarını sırayla işler. Burst + cooldown temposuyla çalışır (N fatura üret
-> X dk dur -> devam), kaldığı yerden devam eder (resumable) ve her batch için
ayrı bir çıktı JSON'una anlık yazar.

Kullanım:
    python -m faz_b_aciklama_uretimi.aciklama_toplu_uret --burst-size 200 --cooldown-min 15 --workers 3
Kesilirse (Ctrl-C / çökme / ertesi gün) aynı komut kaldığı yerden devam eder.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import faz_b_aciklama_uretimi.aciklama_uretim_core as core
from faz_b_aciklama_uretimi.aciklama_uretim_core import (
    OLLAMA_HOST_VARSAYILAN,
    saglayici_ayarla,
    MODEL_VARSAYILAN,
    KATEGORILER,
    tek_fatura_isleme,
    modeli_bellekten_indir,
    yakin_kopya_mi,
    _token_set,
    distinct_n,
)

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"


def durum_yukle(dizin: Path) -> dict:
    with open(dizin / "durum.json", "r", encoding="utf-8") as f:
        return json.load(f)


def durum_kaydet(dizin: Path, durum: dict) -> None:
    with open(dizin / "durum.json", "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=2)


def cikti_yukle(yol: Path) -> dict:
    """Varsa daha önce üretilmiş çıktıları (kayit_id -> kayıt) yükler."""
    if yol.exists():
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def cikti_kaydet(yol: Path, cikti: dict) -> None:
    with open(yol, "w", encoding="utf-8") as f:
        json.dump(cikti, f, ensure_ascii=False, indent=2)


def _batch_no(dosya_adi: str, varsayilan: int) -> int:
    """'batch_0011.json' -> 11. Ad beklenen kalıpta değilse listedeki sırayı kullanır."""
    m = re.search(r"(\d+)", dosya_adi)
    return int(m.group(1)) if m else varsayilan


def _batch_numaralarini_coz(ifade: str) -> set[int]:
    """'11-20', '7', '1,3,5', '1-3,7' -> {numaralar}.

    ÇOK MAKİNELİ üretim için: her makineye ayrık bir aralık verilir, çıktılar
    batch başına ayrı dosyaya (batch_NNNN_ciktilar.json) yazıldığı için yazma
    çakışması olmaz."""
    numaralar: set[int] = set()
    for parca in ifade.split(","):
        parca = parca.strip()
        if not parca:
            continue
        if "-" in parca:
            bas, _, son = parca.partition("-")
            numaralar.update(range(int(bas), int(son) + 1))
        else:
            numaralar.add(int(parca))
    return numaralar


def dilimle(liste: list, boyut: int):
    for i in range(0, len(liste), boyut):
        yield liste[i : i + boyut]


def main():
    parser = argparse.ArgumentParser(description="Toplu açıklama üretimi (burst + cooldown, resumable)")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="batch + durum.json dizini")
    parser.add_argument("--burst-size", type=int, default=200, help="Cooldown öncesi işlenecek fatura sayısı")
    parser.add_argument("--cooldown-min", type=float, default=15.0, help="Burst arası mola (dakika)")
    parser.add_argument("--workers", type=int, default=2, help="Paralel istek sayısı (16GB RAM için 2 önerilir; OLLAMA_NUM_PARALLEL'dan büyük olması FAYDASIZ)")
    parser.add_argument("--model", default=MODEL_VARSAYILAN)
    parser.add_argument("--host", default=OLLAMA_HOST_VARSAYILAN)
    parser.add_argument(
        "--saglayici", choices=["ollama", "groq", "vllm"], default="ollama",
        help="Uretim saglayicisi. 'groq': OpenAI-uyumlu bulut API (GROQ_API_KEY .env'den okunur), "
             "host otomatik ayarlanir, istemci tarafinda 30 istek/dk kota sinirlayici devreye girer.")
    parser.add_argument("--istek-dk", type=int, default=25,
                        help="[groq] Dakikada izin verilen istek. Ucretsiz katmanda istek siniri 30/dk "
                             "ama TOKEN siniri (30K/dk) once baglar: 30 cagri x ~1100 token = 33K > 30K. "
                             "Bu yuzden varsayilan 25.")
    parser.add_argument("--max-batch", type=int, default=0, help="Bu koşuda en fazla kaç batch işlensin (0 = sınırsız)")
    parser.add_argument(
        "--batch", default="",
        help="İşlenecek batch NUMARALARI: '11-20', '7', '1,3,5' ya da karışık '1-3,7'. "
             "ÇOK MAKİNELİ üretim için: her makineye AYRIK bir aralık ver. Bu mod "
             "durum.json'a YAZMAZ (paylaşımlı dosya, makineler birbirini ezerdi); "
             "resume yine çalışır çünkü asıl durum batch_NNNN_ciktilar.json'dadır.",
    )
    parser.add_argument(
        "--dedup-taban", action="append", metavar="DIZIN",
        help="ÖNCEKİ koşuların çıktı dizini; yakın-kopya birikimini tohumlar. "
             "Birden fazla kez verilebilir. Ek/dengeleme koşusu AYRI dizinde "
             "çalıştığında bunu VERMEZSEN önceki metinlerin kopyaları bayraksız "
             "kalır. Yalnız okunur, taban dizine yazılmaz.",
    )
    parser.add_argument("--insan-md", action="store_true", help="İnsan incelemesi için ayrıca MD raporu yaz")
    parser.add_argument("--ilerleme", type=int, default=1000,
                        help="Her N kayıtta bir STDERR'e kilometre taşı bas (0 = kapalı). "
                             "Fatura başına satır STDOUT'a gider; notebook'ta sadece stdout'u "
                             "dosyaya yönlendirirsen (2>&1 YAZMA) ilerleme canlı görünür.")
    parser.add_argument("--sicaklik-tavani", type=float, default=None,
                        help="Kategori sicakliklarina TAVAN uygula (or. 0.9). OpenAI-uyumlu saglayicilarda min_p guvenlik agi olmadigi icin 1.1 ciplak kalir.")
    args = parser.parse_args()

    if args.sicaklik_tavani is not None:
        core.SICAKLIK_TAVANI = args.sicaklik_tavani
        print(f"[+] Sicaklik tavani: {args.sicaklik_tavani}")

    if args.saglayici != "ollama":
        args.host = saglayici_ayarla(args.saglayici, args.istek_dk,
                                     host=None if args.host == OLLAMA_HOST_VARSAYILAN else args.host)
        print(f"[+] Saglayici: {args.saglayici}  model={args.model}  host={args.host}")

    dizin = Path(args.cikti_dizini)
    durum = durum_yukle(dizin)

    # --batch verilirse durum.json SALT OKUNUR olur (bkz. yardım metni).
    durum_yazilabilir = not args.batch
    secili_numaralar = _batch_numaralarini_coz(args.batch) if args.batch else None

    kalan_batchler = []
    for sira, b in enumerate(durum["batchler"], start=1):
        no = _batch_no(b["dosya"], sira)
        if secili_numaralar is not None and no not in secili_numaralar:
            continue
        # Aralık modunda durum.json'daki 'tamam' bayrağı BAŞKA bir makinenin
        # yazdığı olabilir; güvenilir değil. O yüzden aralık verildiğinde bayrak
        # yok sayılır, gerçek durum ciktilar dosyasından okunur.
        if secili_numaralar is None and b["tamam"]:
            continue
        kalan_batchler.append(b)

    # Eksik girdi dosyasını EN BAŞTA yakala: yoksa runner ilk batch'te
    # FileNotFoundError ile düşerdi -- hem de saatler sonra değil, hiç iş
    # yapmadan; ama hangi dosyaların eksik olduğunu söylemeden.
    eksikler = [b["dosya"] for b in kalan_batchler if not (dizin / b["dosya"]).exists()]
    if eksikler:
        print(f"[HATA] {len(eksikler)} batch girdi dosyası bulunamadı: {', '.join(eksikler[:5])}"
              + (" ..." if len(eksikler) > 5 else ""))
        print("       Dizin yanlış olabilir (--cikti-dizini) ya da batch_hazirla.py yeniden koşmalı.")
        return

    if not kalan_batchler:
        print("[✓] İşlenecek batch yok (hepsi tamam ya da --batch aralığı boş).")
        return

    if secili_numaralar is not None:
        print(f"[+] --batch aralığı: {sorted(secili_numaralar)} -> {len(kalan_batchler)} batch")
        print("[i] durum.json'a YAZILMAYACAK (çok makineli mod). Resume ciktilar dosyalarından.")
    print(f"[+] {len(kalan_batchler)} bekleyen batch var (toplam {durum['batch_sayisi']}).")
    print(f"[+] Tempo: {args.burst_size} fatura/burst, {args.cooldown_min} dk cooldown, {args.workers} worker.\n")

    baslangic = time.time()
    genel_uretilen = 0
    genel_retry = 0
    genel_hala_ihlalli = 0
    genel_kategori = Counter()
    islenen_batch = 0
    # Çeşitlilik/dedup ölçümü (run boyunca kategori-içi birikir). Yakın-kopya
    # ÇIKTIYI DÜŞÜRMEZ (resumability + her faturaya açıklama garantisi bozulmasın)
    # -- sadece kayda 'yakin_kopya' bayrağı basar ve raporda görünür kılar.
    kabul_token_setleri: dict[str, list[set[str]]] = {k: [] for k in KATEGORILER}
    kategori_metinleri: dict[str, list[str]] = {k: [] for k in KATEGORILER}
    yakin_kopya_sayaci = 0
    # Birikim normalde sıfırdan başlar ve yalnız BU dizinin çıktılarından
    # beslenir; ayrı dizinde koşan ek üretim önceki metinleri göremez ve
    # kopyaları bayraksız kalır. Taban dizinler yalnız OKUNUR.
    # Beklenen yan etki: tohumlanmış koşuda yakın-kopya oranı yükselir (daha
    # önce görülmeyen gerçek çakışmalar görünür olur), hedef adedi ona göre seç.
    for taban in args.dedup_taban or []:
        tohum = 0
        for yol in sorted(Path(taban).glob("batch_*_ciktilar.json")):
            with open(yol, "r", encoding="utf-8") as f:
                for _k in json.load(f).values():
                    _kat, _m = _k.get("aciklama_kategorisi"), _k.get("aciklama_metni")
                    if _kat in kabul_token_setleri and _m:
                        # YALNIZ dedup birikimi. `kategori_metinleri` koşu sonu
                        # çeşitlilik raporunda kullanılıyor; onu tohumlamak
                        # raporu bozar (rapor bu koşuyu ölçmeli, tohumu değil).
                        kabul_token_setleri[_kat].append(_token_set(_m))
                        tohum += 1
        if tohum:
            dagilim = ", ".join(f"{k}={len(v)}" for k, v in sorted(kabul_token_setleri.items()) if v)
            print(f"[+] dedup tohumu: {taban} -> {tohum} metin ({dagilim})")
        else:
            print(f"[!] dedup tohumu BOŞ: {taban} (yol yanlış olabilir, KONTROL ET)")
    # Model burst boyunca bellekte kalsın; cooldown başında explicit indireceğiz.
    keep_alive = f"{int(args.cooldown_min * 60) + 300}s"

    for batch in kalan_batchler:
        if args.max_batch and islenen_batch >= args.max_batch:
            print(f"[+] --max-batch={args.max_batch} sınırına ulaşıldı, duruluyor.")
            break

        batch_yolu = dizin / batch["dosya"]
        cikti_yolu = dizin / batch["cikti_dosyasi"]
        md_yolu = dizin / batch["cikti_dosyasi"].replace(".json", ".md")

        with open(batch_yolu, "r", encoding="utf-8") as f:
            faturalar = json.load(f)

        cikti = cikti_yukle(cikti_yolu)  # resumability: bitmişleri atla
        # Resume: bu batch'te daha önce üretilenleri dedup birikimine kat ki
        # yakın-kopya bayrağı tutarlı kalsın.
        for _k in cikti.values():
            _kat = _k.get("aciklama_kategorisi")
            _m = _k.get("aciklama_metni")
            if _kat in kabul_token_setleri and _m:
                kabul_token_setleri[_kat].append(_token_set(_m))
                kategori_metinleri[_kat].append(_m)
        kalanlar = [f for f in faturalar if f["kayit_id"] not in cikti]

        print(f"=== {batch['dosya']}: {len(faturalar)} fatura, {len(cikti)} zaten üretilmiş, {len(kalanlar)} kaldı ===")

        if not kalanlar:
            batch["tamam"] = True
            if durum_yazilabilir:
                durum_kaydet(dizin, durum)
            print(f"    (bu batch zaten tamam, işaretlendi)\n")
            continue

        dilimler = list(dilimle(kalanlar, args.burst_size))
        for dilim_no, dilim in enumerate(dilimler, 1):
            dilim_basi = time.time()
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        tek_fatura_isleme, fatura, fatura, args.model, args.host, keep_alive
                    ): fatura
                    for fatura in dilim
                }
                for future in as_completed(futures):
                    fatura, _etiket, metin, hata, ihlaller, deneme_sayisi = future.result()
                    fno = fatura["kayit_id"]
                    kategori = fatura["aciklama_kategorisi"]

                    if hata or metin is None:
                        print(f"    [X] {fno} - HATA: {hata or 'Metin boş'}")
                        continue

                    # Dedup: aynı kategoride kabul edilenlere çok benziyorsa
                    # işaretle (düşürme). yetersiz'de tekrar doğal ('iş gideri'
                    # vb.) -> eşik daha gevşek.
                    dedup_esik = 0.95 if kategori == "yetersiz" else 0.8
                    yakin = yakin_kopya_mi(metin, kabul_token_setleri.get(kategori, []), dedup_esik)
                    if yakin:
                        yakin_kopya_sayaci += 1
                    if kategori in kabul_token_setleri:
                        kabul_token_setleri[kategori].append(_token_set(metin))
                        kategori_metinleri[kategori].append(metin)

                    cikti[fno] = {
                        "aciklama_metni": metin,
                        "aciklama_kategorisi": kategori,
                        "deneme_sayisi": deneme_sayisi,
                        "kalan_ihlaller": ihlaller,
                        "yakin_kopya": yakin,
                    }
                    genel_uretilen += 1
                    genel_kategori[kategori] += 1
                    if deneme_sayisi == 2:
                        genel_retry += 1
                        if ihlaller:
                            genel_hala_ihlalli += 1

                    # Her fatura bitince hemen diske yaz + yazdır -- burst
                    # sonunu beklemeden anlık ilerleme görülsün ve kesintide
                    # (Ctrl-C/çökme) o ana kadar üretilenler kaybolmasın.
                    cikti_kaydet(cikti_yolu, cikti)
                    ihlal_notu = f", kalan_ihlal={ihlaller}" if ihlaller else ""
                    yakin_notu = ", yakin_kopya" if yakin else ""
                    print(f"    [{genel_uretilen}] {fno} ({kategori}, deneme={deneme_sayisi}{ihlal_notu}{yakin_notu})")

                    # KİLOMETRE TAŞI -> STDERR. Fatura başına satır (stdout)
                    # notebook'ta 25.000 satır demek, o yüzden log dosyasına
                    # yönlendiriliyor; ama o zaman koşu boyunca HİÇBİR ŞEY
                    # görünmüyor ve "çöktü mü?" sorusu cevapsız kalıyor.
                    # Ayrı akışa basınca stdout dosyaya giderken bu canlı kalır.
                    if args.ilerleme and genel_uretilen % args.ilerleme == 0:
                        gecen_dk = (time.time() - baslangic) / 60
                        hiz = genel_uretilen / gecen_dk if gecen_dk else 0
                        print(f"[ilerleme] {genel_uretilen} kayit | {gecen_dk:.0f} dk | "
                              f"{hiz:.0f} kayit/dk | retry {genel_retry}",
                              file=sys.stderr, flush=True)

            gecen = time.time() - dilim_basi
            print(f"    [dilim {dilim_no}/{len(dilimler)}] {len(dilim)} fatura işlendi ({gecen:.0f} sn), toplam üretilen: {genel_uretilen}")

            # Son dilim değilse cooldown (cooldown_min<=0 ise tamamen atlanır --
            # pilot script'teki gibi model hiç indirilmeden sıcak kalmaya devam eder)
            if dilim_no < len(dilimler) and args.cooldown_min > 0:
                print(f"    [cooldown] model bellekten indiriliyor, {args.cooldown_min} dk mola...")
                modeli_bellekten_indir(args.model, args.host)
                time.sleep(args.cooldown_min * 60)

        # Batch tamam
        batch["tamam"] = True
        batch["tamamlanma_zamani"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if durum_yazilabilir:
            durum_kaydet(dizin, durum)
        islenen_batch += 1

        if args.insan_md:
            _md_yaz(md_yolu, faturalar, cikti, args.model)

        print(f"=== {batch['dosya']} TAMAM ===\n")

        # Batch'ler arası da cooldown (son işlenen batch değilse, iş kaldıysa ve cooldown_min>0 ise)
        kalan_var = any(not b["tamam"] for b in durum["batchler"])
        sinir_var = args.max_batch and islenen_batch >= args.max_batch
        if kalan_var and not sinir_var and args.cooldown_min > 0:
            print(f"[cooldown] batch arası, model indiriliyor, {args.cooldown_min} dk mola...\n")
            modeli_bellekten_indir(args.model, args.host)
            time.sleep(args.cooldown_min * 60)

    # Özet
    gecen = time.time() - baslangic
    dk, sn = divmod(gecen, 60)
    kalan_toplam = sum(1 for b in durum["batchler"] if not b["tamam"])
    print("\n" + "=" * 50)
    print(f"Bu koşuda üretilen açıklama: {genel_uretilen}")
    print(f"Retry tetiklenen: {genel_retry} (bunlardan {genel_hala_ihlalli} tanesi 2. denemede de ihlalli)")
    print(f"Kategori dağılımı: {dict(genel_kategori)}")
    print(f"Yakın-kopya işaretlenen (düşürülmedi): {yakin_kopya_sayaci}")
    print("Çeşitlilik (distinct-1 / distinct-2, 1'e yakın = çeşitli):")
    for kat in KATEGORILER:
        metinler = kategori_metinleri.get(kat, [])
        if not metinler:
            continue
        print(f"  {kat:12s} n={len(metinler):5d} | distinct-1={distinct_n(metinler, 1):.3f} "
              f"distinct-2={distinct_n(metinler, 2):.3f}")
    print(f"Kalan batch sayısı: {kalan_toplam}")
    print(f"Geçen süre (cooldown dahil): {int(dk)} dk {int(sn)} sn")
    if kalan_toplam == 0:
        print("Tüm batch'ler tamamlandı! Sonraki adım: python -m faz_b_aciklama_uretimi.aciklama_birlestir")
    else:
        print("Devam etmek için aynı komutu tekrar çalıştır (kaldığı yerden devam eder).")
    print("=" * 50)


def _md_yaz(md_yolu: Path, faturalar: list[dict], cikti: dict, model: str) -> None:
    from faz_b_aciklama_uretimi.aciklama_uretim_core import kalemler_ozetle
    with open(md_yolu, "w", encoding="utf-8") as f:
        f.write(f"# Ollama ({model}) — {md_yolu.stem}\n\n---\n\n")
        for idx, fatura in enumerate(faturalar, 1):
            kayit = cikti.get(fatura["kayit_id"])
            if not kayit:
                continue
            uyari = ""
            if kayit["kalan_ihlaller"]:
                uyari = f"*⚠️ kalan ihlaller: {kayit['kalan_ihlaller']}*\n\n"
            f.write(f"## {idx}. {fatura['fatura_no']}\n\n")
            f.write(f"- **Kategori:** `{kayit['aciklama_kategorisi']}`\n")
            f.write(f"- **Anomali Türleri:** `{fatura['anomali_turleri']}`\n\n")
            f.write(f"**Kalemler:**\n{kalemler_ozetle(fatura['kalemler'])}\n\n")
            f.write(f"**Üretilen Açıklama:**\n> {kayit['aciklama_metni']}\n\n")
            f.write(uyari)
            f.write("---\n\n")


if __name__ == "__main__":
    main()
