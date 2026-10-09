import uuid
import random
from decimal import Decimal
from faker import Faker
from datetime import date, datetime, time, timedelta, timezone
from ortak.schema import IS_KOLU_KATEGORILERI, HarcamaKategorisi, FaturaKalemi, Fatura, IsKolu, FirmaTuru, KDV_ORANI_MAP
from ortak.politika import kalem_limiti
import re, math, unicodedata
from collections.abc import Iterator
from pathlib import Path


def _birlesen_nokta_temizle(metin: str) -> str:
    """'Akdemi̇r' -> 'Akdemir', 'Polo Si̇lver' -> 'Polo Silver'.

    IKI KAYNAKTAN geliyor ve ikisi de fise/prompt'a sizdi (2026-07-30'da olculdu):
      1. Soyisim listesi 'İ'yi AYRISMIS tutuyor ('i' + U+0307) -> TUM_SOYISIMLER'in
         %21'i. Sentetik firma adlari registry'de danismanlik %76 / lojistik %83
         paya sahip oldugu icin fis satici adlarina genis olcude yansiyordu.
      2. Market CSV yukleyicilerindeki `.title()` -- Python 'İ'yi kucultunce
         'i' + U+0307 uretiyor. Kalem adlarinin %4-20'si bozuktu:
         ofis_sarf_malzeme %20,3 | temizlik %9,4 | tutun %8,6 | temel_gida %4,3
         ('Polo Si̇lver', 'Danone Danette 2'Li̇m', 'Hd Altin Yaprak Si̇gara').

    Ekranda 'i' ile AYNI gorunur, o yuzden gozle fark edilmiyor; ayri bir kod
    noktasi oldugu icin modele oyle gidiyor. NFC mesru 'İ'yi (U+0130) geri
    birlestirir, kucuk 'i'de birlesecek karakter olmadigi icin nokta silinir --
    yani bu donusum 'İstanbul'u BOZMAZ, yalnizca bozuk olani duzeltir.

    NOT: fonksiyon dosyanin BASINDA tanimli olmali -- market yukleyicileri modul
    yuklenirken (asagida) cagriliyor ve bunu kullaniyor.
    """
    return unicodedata.normalize("NFC", metin).replace("̇", "")

fake = Faker("tr_TR")
SOYISIM_SQL_DOSYASI = Path(__file__).parent.parent.parent / "data" / "soyisimler.sql"
MARKET_URUNLERI_CSV = Path(__file__).parent.parent.parent / "data" / "urun_verileri" / "market_urunleri.csv"
TEMIZ_URUNLER_CSV = Path(__file__).parent.parent.parent / "data" /   "urun_verileri" / "temiz_urunler.csv"
YEMEK_URUNLERI_CSV = Path(__file__).parent.parent.parent / "data" /  "urun_verileri" /"restoran_urunleri.csv"
DANISMANLIK_URUNLERI_CSV = Path(__file__).parent.parent.parent / "data" /  "hizmet_verileri" /"danismanlik_urunleri.csv"
KONAKLAMA_URUNLERI_CSV = Path(__file__).parent.parent.parent / "data" /  "hizmet_verileri" /"konaklama_urunleri.csv"
ULASIM_URUNLERI_CSV = Path(__file__).parent.parent.parent / "data" /  "hizmet_verileri" /"ulasim_urunleri.csv"
ANOMALI_URUNLERI_CSV = Path(__file__).parent.parent.parent / "data" / "anomali_verileri" / "anomali_urunler.csv"

# CSV kategorisi -> HarcamaKategorisi eşlemesi. Karşılığı olmayan satırlar
# (KAĞIT/EV/BEBEK/PET gibi şu anki şemada kategorisi bulunmayanlar) BİLİNÇLİ
# olarak eşlemeye dahil EDİLMEDİ, aksi halde TEMEL_GIDA'ya sızarlardı.
# SİGARA de KASITLI olarak burada YOK: bu ürünler sadece anomali_urunleri_yukle()
# üzerinden TUTUN_URUNLERI anomali havuzuna girmeli, normal üretimde asla kullanılmamalı.
MARKET_KATEGORI_ESLESTIRME: dict[str, HarcamaKategorisi] = {
    "GIDA": HarcamaKategorisi.TEMEL_GIDA,
    "MEYVE SEBZE": HarcamaKategorisi.TEMEL_GIDA,
    "SÜT KAHVALTILIK": HarcamaKategorisi.TEMEL_GIDA,
    "İÇECEK": HarcamaKategorisi.TEMEL_GIDA,
    "ET TAVUK": HarcamaKategorisi.TEMEL_GIDA,
    "DETERJAN TEMİZLİK": HarcamaKategorisi.TEMIZLIK,
    "KOZMETİK": HarcamaKategorisi.KISISEL_BAKIM,
}


def _market_satirlari(dosya_yolu: Path) -> Iterator[tuple[str, str, int]]:
    """Market ozet CSV'sini (urun_adi, csv_kategori, siklik, alt_tipler) tek
    bicime indirger: (urun_adi, csv_kategori, siklik).

    2026-08-21'den beri `market_urunleri.csv` DOGRUDAN ozet formati (9.262
    benzersiz urun + sıklık kolonu) -- eskiden ayri bir "_ozet" dosyasi tercih
    edilirdi, artik tek dosya var. Ham kasa dokumu (603.545 satir, tekrar
    ICEREN) artik uretimde OKUNMUYOR, `data/backups/urun_verileri/
    market_urunleri_yedek.csv`'de arsivlendi -- ozet yeniden uretilmesi
    gerekirse `market_ozet_olustur.py --girdi <yedek yolu>` ile oradan
    turetilir (bkz. o dosyanin HAM_CSV sabiti). Cagiran havuzu `siklik` kadar
    tekrarlayarak doldurur -> secim dagilimi ham dosyayla AYNI (tekrar ortuk
    agirliktir, silinmez)."""
    import csv as _csv
    if not dosya_yolu.exists():
        return
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        for satir in _csv.DictReader(f):
            isim = (satir.get("urun_adi") or "").strip()
            if isim:
                yield isim, (satir.get("csv_kategori") or "").strip(), int(satir["siklik"])


def market_urunleri_yukle(dosya_yolu: Path = MARKET_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[str]]:
    """
    Market ozet CSV'sinden (urun_adi, csv_kategori) kategoriye göre GRUPLANMIŞ
    açıklama havuzu üretir. MARKET_KATEGORI_ESLESTIRME'de karşılığı olmayan
    satırlar (SİGARA dahil) elenir -- artık tek bir düz TEMEL_GIDA listesine
    karışmıyorlar. Dosya yoksa boş dict döner.
    """
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    for isim, kategori_str, siklik in _market_satirlari(dosya_yolu):
        hedef_kategori = MARKET_KATEGORI_ESLESTIRME.get(kategori_str.upper())
        if hedef_kategori is None:
            continue
        havuzlar.setdefault(hedef_kategori, []).extend(
            [_birlesen_nokta_temizle(isim.title())] * siklik)
    return havuzlar


def market_ek_kategoriler_yukle(
    dosya_yolu: Path = MARKET_URUNLERI_CSV,
) -> tuple[dict[HarcamaKategorisi, list[str]], list[str]]:
    """market_urunleri.csv'nin ESLENMEYEN kategorilerinden geri kazanilanlar.

    Doner: (havuzlar, sigara_adlari)
      - havuzlar: TUTUN_URUNLERI (SİGARA, tamami) + TEMIZLIK/OFIS_SARF_MALZEME
        (KAĞIT/EV, beyaz liste suzgeciyle)
      - sigara_adlari: makullik haritasina `market` olarak islenecek adlar
        (§16 politika/satici eksen ayrimi -- markette sigara satici acisindan
        OLAGANDIR, yalniz politika ihlalidir; bu liste olmadan 19 bin kalem
        `is_kolu_kategori_uyumsuzlugu`'nu da yanlisligla tetiklerdi).
    Dosya yoksa bos doner (mevcut davranis korunur).
    """
    if not dosya_yolu.exists():
        _csv_yok_uyar(dosya_yolu, "market ek kategorileri (SİGARA/KAĞIT/EV)")
        return {}, []
    beyaz = {k: [(kat, re.compile(d, re.IGNORECASE)) for kat, d in eslemeler]
             for k, eslemeler in MARKET_SARF_BEYAZ_LISTE.items()}
    kara = re.compile(MARKET_SARF_KARA_LISTE, re.IGNORECASE)
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    sigaralar: list[str] = []
    for isim, kategori_str, siklik in _market_satirlari(dosya_yolu):
        kategori_str = kategori_str.upper()
        baslikli = _birlesen_nokta_temizle(isim.title())
        yasakli = MARKET_YASAKLI_ESLESTIRME.get(kategori_str)
        if yasakli is not None:
            havuzlar.setdefault(yasakli, []).extend([baslikli] * siklik)
            sigaralar.extend([baslikli] * siklik)
            continue
        eslemeler = beyaz.get(kategori_str)
        if not eslemeler or kara.search(isim):
            continue
        for hedef, desen in eslemeler:     # ilk eslesen kazanir
            if desen.search(isim):
                havuzlar.setdefault(hedef, []).extend([baslikli] * siklik)
                break
    return havuzlar, sigaralar

# market_urunleri.csv'nin ESLENMEYEN kategorileri (2026-07-30'da olculdu):
#   SİGARA 19.031 | KAĞIT 21.577 | EV 12.816 | BEBEK 6.382 | PET 33
# BEBEK/PET dogru sekilde dusuruluyor. Diger ucu asagida geri kazaniliyor.
#
# SİGARA -> TUTUN_URUNLERI (yasakli kategori). Kazanc GERCEKCILIK: elle yazilmis
# tutun havuzu 18 kalemdi ve "Sigara", "Ithal Sigara" gibi jenerik adlardan
# olusuyordu. CSV ise gercek marka adi veriyor: 'MONTE CARLO SLENDER BLUE',
# 'WINSTON KISA BOX', 'PARLIAMENT UZUN NIGHT BLUE'. Fiste marka gorunmesi hem
# gorsel fis hem Faz B aciklamasi icin daha inandirici.
# NOT: CSV'de ALKOL YOK (tarandi: bira/raki/sarap/votka anahtar kelimeleriyle 0
# eslesme) -> alkol `anomali_urunler.csv`'den gelmeye DEVAM eder.
MARKET_YASAKLI_ESLESTIRME = {
    "SİGARA": HarcamaKategorisi.TUTUN_URUNLERI,
}

# KAĞIT ve EV: buyuk kismi gercekten KURUMSAL sarf malzemesi ('F Saff Havlu
# Kagidi', 'Teno 100 Adet Pecete', 'Cook Cop Torbasi', 'Piknik Plastik Eldiven',
# 'Duracell 4'lu Kalem Pil') ama icine kisisel/ev urunleri karismis ('Orkid
# Ultra Comfort Gece' = ped, 'Sayan Dikissiz Corap', 'Cakmak Mutfak'). O yuzden
# TOPTAN alinmaz, BEYAZ LISTE ile alinir -- urun_kurumsal_filtre.py'nin
# yaklasimi (§17.1): havuz ham B2C oldugunda elemeye calismak yerine kabul
# edileni saymak gerekiyor.
#
# Faz B'ye faydasi: bu adlar temiz_urunler.csv'nin (Trendyol) adlarindan cok
# daha kolay anlatilir -- 'Havlu Kagidi' vs 'dijiname Dijital Hazir Kartvizit+tag - 21'.
# CSV kategorisi -> [(hedef kategori, urun deseni), ...]  SIRA ONEMLI, ilk eslesen kazanir.
#
# EV KATEGORISI BOLUNDU (2026-07-30, ilk surumun yan etkisi olculdukten sonra):
# once EV'in tamami OFIS_SARF_MALZEME'ye eslenmisti ve sonuc gercek disi cikti --
# 1.011 kirtasiye faturasinin en sik kalemleri cop poseti/buzdolabi poseti/kalem
# pil oldu (olculdu). Ustune firma adi kisiti kirtasiyeciyi zaten yalniz
# OFIS_SARF_MALZEME'ye daralttigi icin o fisler %100 cop posetine dondu --
# duzeltmeye calistigim "dijital kartvizit baskinligi"nin daha kotusu.
# Dogrusu: cop torbasi/poset/eldiven/sunger/strec TESIS-TEMIZLIK malzemesidir,
# kirtasiye degil. Yalniz pil/bant/yapistirici ofis sarfina aittir.
MARKET_SARF_BEYAZ_LISTE: dict[str, list[tuple[HarcamaKategorisi, str]]] = {
    "KAĞIT": [(
        HarcamaKategorisi.TEMIZLIK,
        r"havlu ?ka[gğ]|pe[cç]ete|tuvalet ka[gğ]|kagit havlu|ka[gğ][ıi]t havlu"
        r"|[ıi]slak mendil|mendil|rulo|z ?katlama|dispenser",
    )],
    "EV": [
        (HarcamaKategorisi.TEMIZLIK,
         r"[cç][oö]p torba|[cç][oö]p po[sş]et|buzdolab[ıi] po[sş]|po[sş]et"
         r"|eldiven|bula[sş][ıi]k s[uü]nger|s[uü]nger|ovma|bula[sş][ıi]k teli"
         r"|streç film|al[uü]minyum folyo"),
    ],
    # EV -> OFIS_SARF_MALZEME eslemesi (pil/bant) KALDIRILDI: CSV'de ~580 pil
    # varyanti var ve kuratorlu 34 kirtasiye kalemini bogdu -- olculdu, kirtasiye
    # fisinin en sik 6 kaleminin ALTISI DA 'Kalem Pil'di. Turk kirtasiyesi pil
    # satar ama fisin tamami pil olmaz; havuz orani yanlisti. Pil artik
    # BEBEK/PET gibi dusuruluyor.
}

# Beyaz listeden gecse bile bunlar KISISEL/EV urunudur, elenir (kara liste ONCE
# calisir -- §17.1'deki kalibrasyon tuzagi: ayni kelime iki listede olursa urun elenir).
MARKET_SARF_KARA_LISTE = (
    r"ped\b|hijyenik|orkid|molped|tampon|bebek bezi|[cç]ocuk bezi|prezervatif"
    r"|[cç]orap|iç ?[cç]ama[sş][ıi]r|mutfak\b|kedi|k[oö]pek|oyuncak"
)


#helper method ürün adında gereksiz şeyleri temizler.
def _urun_kodu_temizle(baslik: str) -> str:
    """
    Başlık sonundaki SKU/ürün kodu benzeri token'ları temizler.
    Heuristik: sondan başlayarak, içinde rakam geçen kelimeleri siler.
    Örn: 'Kruvaze Yaka Trençkot 20mtegk1955trn00' -> 'Kruvaze Yaka Trençkot'
         'Külot TYC0051198584' -> 'Külot'
    Not: bazen 'PNR 002' gibi durumlarda tek harfli kısaltma (PNR) rakamsız
    olduğu için silinmeden kalabilir — mükemmel değil ama çoğu durumda işi görür.
    """
    kelimeler = baslik.split()
    while kelimeler and re.search(r"\d", kelimeler[-1]) and len(kelimeler[-1]) >= 3:
        kelimeler.pop()
    temiz = " ".join(kelimeler).strip(" -/")
    return temiz if temiz else baslik


def temiz_urunleri_yukle(dosya_yolu: Path = TEMIZ_URUNLER_CSV) -> dict[HarcamaKategorisi, list[str]]:
    """
    veri_temizle.py tarafından üretilen, her satırı zaten tek bir
    HarcamaKategorisi'ne atanmış temiz CSV'yi okur. Burada ARTIK keyword
    matching / regex YOK — sınıflandırma kararı offline'da (veri_temizle.py)
    verildi, bu fonksiyon sadece harcama_kategorisi sütununa göre gruplayarak okuyor.
    """
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    if not dosya_yolu.exists():
        return havuzlar
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            baslik = (satir.get("title") or "").strip()
            kategori_str = (satir.get("harcama_kategorisi") or "").strip()
            if not baslik or not kategori_str:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue
            havuzlar.setdefault(kategori, []).append(_urun_kodu_temizle(baslik))
    return havuzlar

# Firma adi belirli bir mutfagi isaret ettiginde SADECE o firmada gorunmesi
# gereken menu bolumleri. Genel YEMEK_HIZMETI havuzuna ALINMAZLAR: boylece
# notr adli bir restoran ("Mola Lokantasi") sushi/cig kofte satmaz.
# Ters yon (burgercinin tavuk sis satmamasi) mutfak kisitiyla saglanir.
DAR_MUTFAK_BOLUMLERI = {"cigkofte", "uzakdogu", "pastane_tatli", "balik", "pizza", "burger",
                        "borek", "kokorec"}


def yemek_urunleri_yukle(dosya_yolu: Path = YEMEK_URUNLERI_CSV) -> list[str]:
    """
    Restoran/yemek harcamasi icin hazirlanan temiz CSV'yi (kategori, urun_adi
    sutunlari) okur ve GENEL havuzu dondurur. DAR_MUTFAK_BOLUMLERI atlanir --
    o urunler yalnizca adi eslesen firmada, mutfak kisiti uzerinden gorunur.
    Dosya yoksa bos liste doner, sabit havuz tek basina devrede kalir.
    """
    import csv as _csv
    if not dosya_yolu.exists():
        return []
    urunler: list[str] = []
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            urun = (satir.get("urun_adi") or "").strip()
            bolum = (satir.get("kategori") or "").strip()
            if urun and bolum not in DAR_MUTFAK_BOLUMLERI:
                urunler.append(urun)
    return urunler


def yemek_urunleri_bolumlu_yukle(dosya_yolu: Path = YEMEK_URUNLERI_CSV) -> dict[str, list[str]]:
    """Ayni CSV'yi bolum -> [urun] olarak yukler (mutfak kisiti icin). Genel
    havuzun aksine DAR bolumler de dahildir."""
    import csv as _csv
    if not dosya_yolu.exists():
        return {}
    bolumler: dict[str, list[str]] = {}
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        for satir in _csv.DictReader(f):
            urun = (satir.get("urun_adi") or "").strip()
            bolum = (satir.get("kategori") or "").strip()
            if urun and bolum:
                bolumler.setdefault(bolum, []).append(urun)
    return bolumler

def danismanlik_urunleri_yukle(dosya_yolu: Path = DANISMANLIK_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[tuple[str, str | None]]]:
    """Danışmanlık harcamaları için hazırlanan temiz CSV'yi okur (urun_adi, birim çiftleriyle)."""
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[tuple[str, str | None]]] = {}
    if not dosya_yolu.exists():
        return havuzlar

    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            birim = (satir.get("birim") or "").strip() or None

            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue

            havuzlar.setdefault(kategori, []).append((urun, birim))

    return havuzlar


def konaklama_urunleri_yukle(dosya_yolu: Path = KONAKLAMA_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[tuple[str, str | None]]]:
    """Konaklama harcamaları için hazırlanan temiz CSV'yi okur (urun_adi, birim çiftleriyle)."""
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[tuple[str, str | None]]] = {}
    if not dosya_yolu.exists():
        return havuzlar

    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            birim = (satir.get("birim") or "").strip() or None

            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue

            havuzlar.setdefault(kategori, []).append((urun, birim))

    return havuzlar


def ulasim_urunleri_yukle(dosya_yolu: Path = ULASIM_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[tuple[str, str | None]]]:
    """Ulaşım harcamaları için hazırlanan temiz CSV'yi okur (urun_adi, birim çiftleriyle)."""
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[tuple[str, str | None]]] = {}
    if not dosya_yolu.exists():
        return havuzlar

    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            birim = (satir.get("birim") or "").strip() or None

            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue

            havuzlar.setdefault(kategori, []).append((urun, birim))

    return havuzlar
def _csv_yok_uyar(dosya_yolu: Path, sonuc: str) -> None:
    """
    Eksik CSV'yi GURULTULU bildirir.

    SESSIZ DUSMEK PAHALIYA MAL OLDU (2026-07-29): `data/anomali_veri` dizini
    `data/anomali_verileri` olarak yeniden adlandirilinca (gitignore istisnasina
    uydurmak icin) bu dosya bulunamadi. Iki yukleyici de sessizce bos dondu:
      - ACIKLAMA_HAVUZU yasakli kategorileri 3-4 elemanli sabit listeye dustu
      - ANOMALI_URUN_MAKULLUGU bos kaldi -> satici-ekseni ayrimi devre disi
    Sonuc: 100k'lik bir uretim, yasakli kategoride 4 urun cesitliligiyle ve
    %100 etiket ortusmesiyle tamamlandi -- TEK BIR HATA MESAJI OLMADAN.

    Geri dusme davranisi KORUNUYOR (kirilgan import zinciri istemiyoruz) ama
    artik sessiz degil.
    """
    import warnings
    warnings.warn(
        f"\n  CSV BULUNAMADI: {dosya_yolu}\n  -> {sonuc}\n"
        "  -> uretime devam etmeden once yolu duzeltin.",
        RuntimeWarning, stacklevel=3,
    )


def anomali_urunleri_yukle(dosya_yolu: Path = ANOMALI_URUNLERI_CSV) -> dict[HarcamaKategorisi, list[str]]:
    """
    anomalili_veriler.csv'yi (kategori, urun_adi sutunlari) okur. kategori
    sutunundaki degerler (alkol, tutun_urunleri, eglence, kumar)
    HarcamaKategorisi enum degerleriyle birebir eslesiyor, ayri bir
    eslestirme tablosu gerekmiyor. Dosya yoksa bos dict doner, mevcut
    sabit havuzlar (ACIKLAMA_HAVUZU icindeki elle yazilmis 3-4 elemanli
    listeler) devrede kalir.
    """
    import csv as _csv
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    if not dosya_yolu.exists():
        _csv_yok_uyar(dosya_yolu, "yasakli kategori havuzlari 3-4 elemanli sabit listeye DUSUYOR")
        return havuzlar
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            kategori_str = (satir.get("kategori") or "").strip().lower()
            urun = (satir.get("urun_adi") or "").strip()
            if not kategori_str or not urun:
                continue
            try:
                kategori = HarcamaKategorisi(kategori_str)
            except ValueError:
                continue
            havuzlar.setdefault(kategori, []).append(urun)
    return havuzlar


def anomali_urun_makullugu_yukle(dosya_yolu: Path = ANOMALI_URUNLERI_CSV) -> dict[str, set[str]]:
    """
    Ayni CSV'nin ucuncu sutunu (`makul_is_kollari`, `;` ile ayrilmis is_kolu
    listesi) -> {urun_adi: {is_kolu, ...}}.

    NEDEN AYRI BIR EKSEN: yasakli kategoriler (alkol/tutun/eglence/kumar)
    hicbir is kolunun IS_KOLU_KATEGORILERI listesinde YOK -- olamaz da, olsaydi
    TEMIZ fise alkol/sigara duserdi. Bunun yan etkisi olarak yasakli bir kalem
    enjekte edilir edilmez `is_kolu_kategori_uyumsuzlugu` de yapisal olarak
    tetikleniyordu (olculdu: 2065/2065, %100). Oysa bunlar IKI AYRI eksendir:
      - POLITIKA ekseni  : sirket bu gideri odemez            -> yasakli_kategori
      - SATICI ekseni    : bu dukkan bunu zaten satmaz        -> is_kolu_kategori_uyumsuzlugu
    Restoranda alkol, markette sigara, akaryakit istasyonunda piyango bileti
    POLITIKA ihlalidir ama satici acisindan tamamen olagandir. Iki etiket %100
    korele oldugunda biri bilgi tasimaz; model aralarindaki farki ogrenemez.

    Makullik URUN bazindadir, kategori bazinda DEGIL: markette "Milli Piyango
    Bileti" makul, "Casino Cip Alimi" degil; restoranda "Nargile" makul, "Sarma
    Tutun (50gr)" degil. Kategori bazli bir muafiyet bu ayrimi silerdi.

    Sutun bos ise urun hicbir is koluna makul degildir (destinasyon biletleri,
    casino urunleri) -- bunlar uyumsuzlugu tetiklemeye DEVAM eder. Dosya ya da
    sutun yoksa bos dict doner: davranis eski haline (her yasakli kalem
    uyumsuzluk tetikler) guvenli sekilde geri duser.
    """
    import csv as _csv
    makullik: dict[str, set[str]] = {}
    if not dosya_yolu.exists():
        _csv_yok_uyar(dosya_yolu, "satici-ekseni ayrimi DEVRE DISI (yasakli kalem = otomatik is_kolu uyumsuzlugu)")
        return makullik
    with open(dosya_yolu, "r", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        for satir in reader:
            urun = (satir.get("urun_adi") or "").strip()
            ham = (satir.get("makul_is_kollari") or "").strip()
            if not urun or not ham:
                continue
            makullik[urun] = {p.strip() for p in ham.split(";") if p.strip()}
    return makullik


# 2.1 — Kategoriye göre açiklama havuzu

ACIKLAMA_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: [
        "Öğle Yemeği Servis",
        "İş Yemeği",
        "Catering Hizmeti",
        "Tavuk Şiş Menü",
        "Izgara Tabaği",
        "Restoran Faturasi",
        "Personel Yemek Bedeli",
        "Toplanti Yemek Servisi",
        "Kahvalti Servisi",
    ],
    HarcamaKategorisi.TEMEL_GIDA: [
        "Tavuk But (Kg)",
        "Dana Kiyma",
        "Kuşbaşi Et",
        "Market Alişverişi",
        "Bakliyat",
        "Süt ve Süt Ürünleri",
        "Ekmek",
        "Gida Ürünleri",
        "Sebze ve Meyve",
    ],
    HarcamaKategorisi.ULASIM_HIZMETI: [
    "Nakliye Ücreti", "Kargo Hizmeti", "Taşimacilik Bedeli",
    "Depolama Hizmeti", "Lojistik Hizmet Bedeli",
    ],
    HarcamaKategorisi.ULASIM_BIREYSEL: [
        "Taksi Ücreti", "Uçak Bileti", "Otopark Ücreti",
        "Yakit Gideri", "Otobüs/Metro Karti Yükleme", "Araç Kiralama",
    ],
    HarcamaKategorisi.KONAKLAMA: [
        "Otel Konaklama",
        "Misafirhane Ücreti",
        "Apart Kiralama",
        "Konaklama Hizmeti",
    ],
    # KÜRATÖRLÜ ÇEKİRDEK (2026-07-30'da 5 -> 34 kaleme çıkarıldı).
    # Gerekçe: havuzun CSV tarafı (temiz_urunler.csv) dijital kartvizite,
    # market EV kategorisi ise pil/banda yığılıyordu. Ölçüldü: firma adı kısıtı
    # kırtasiyeciyi bu kategoriye daralttığında fişin dört kalemi de kartvizit,
    # sonra da hepsi 'Kalem Pil' çıkıyordu -- gerçek bir kırtasiye fişi kalem,
    # kağıt, dosya, klasör, toner içerir. Çekirdek listesi o tabanı verir.
    HarcamaKategorisi.OFIS_SARF_MALZEME: [
        "Kirtasiye Malzemesi", "Toner/Kartuş", "Yazici Kağidi", "A4 Kağit Kolisi",
        "A4 Fotokopi Kağidi (500'lü)", "A3 Fotokopi Kağidi", "Renkli Fotokopi Kağidi",
        "Tükenmez Kalem (10'lu)", "Kurşun Kalem Kutusu", "Versatil Kalem",
        "Fosforlu Kalem Seti", "Beyaz Tahta Kalemi", "Permanent Marker",
        "Silgi", "Kalemtiraş", "Cetvel Seti",
        "A4 Defter", "Spiralli Defter", "Bloknot", "Yapişkanli Not Kağidi",
        "Telli Dosya", "Plastik Dosya (50'li)", "Klasör (Genişletilebilir)",
        "Arşiv Kutusu", "Evrak Zarfi (100'lü)", "Kraft Zarf",
        "Zimba Makinesi", "Zimba Teli", "Delgeç", "Ataş Kutusu", "Klips Seti",
        "Yapiştirici Stick", "Koli Bandi", "Şeffaf Bant",
    ],
    # KÜRATÖRLÜ ÇEKİRDEK LİSTE (2026-07-30'da 5 -> 40 kaleme çıkarıldı).
    # Gerekçe: bu kategorinin CSV kaynağı (temiz_urunler.csv / Trendyol) özünde EV
    # mobilyası. §17.1 zaten 4196 -> 248'e budamıştı ama kalanın ~%55'i de ev
    # ürünüydü ("Buzdolabı İçi Organizer", "Bavul İçi Düzenleyici", "Altın Takı
    # Standı", "Ağaç Lambader", "Balkon Bahçe Oturma Grubu"). Jenerik beyaz-liste
    # terimleri (`raf`, `organizer`, `duzenleyici`) bunları geçiriyordu -- §17.1'in
    # "kalibrasyon tuzağı" notunun `sunum`/`kasa`/`çekmece` için söylediğinin aynısı.
    # Kara-liste yarışı 249 kalemlik bir havuzda verimsiz; onun yerine GERÇEKTEN
    # ofis mobilyası olan bir çekirdek yazıldı. CSV'den süzülen kalan kalemler
    # buna EKLENİR (aşağıdaki birleştirme bloğu), yani çeşitlilik korunur ama
    # havuzun tamamı artık ev dekorasyonuna teslim değil.
    HarcamaKategorisi.OFIS_MOBILYA: [
        "Ofis Masasi", "Çalişma Masasi", "Toplanti Masasi", "Makam Masasi",
        "L Tipi Çalişma Masasi", "Yükseklik Ayarli Çalişma Masasi",
        "Ofis Sandalyesi", "Ergonomik Ofis Koltuğu", "Yönetici Koltuğu",
        "Misafir Sandalyesi", "Bekleme Koltuğu", "Toplanti Sandalyesi",
        "Tabure", "Bar Tabure",
        "Dosya Dolabi", "Evrak Dolabi", "Kilitli Dolap", "Arşiv Dolabi",
        "Çekmeceli Keson", "Keson", "Kitaplik", "Etajer", "Dosya Rafi",
        "Beyaz Tahta", "Yazi Tahtasi", "Duyuru Panosu", "Flipchart",
        "Projeksiyon Perdesi", "Portmanto", "Askilik", "Şemsiyelik",
        "Masa Lambasi", "Monitör Standi", "Laptop Standi", "Klavye Altliği",
        "Evrak Rafi", "Kalemlik", "Masa Üstü Düzenleyici",
        "Kahve Makinesi", "Su Sebili",
    ],
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: [
        "Yazici/Tarayici", "Monitör", "Bilgisayar",
        "Klavye/Mouse Seti", "Sunucu Ekipmani",
    ],
    HarcamaKategorisi.YAZILIM_LISANS: [
        "Yazilim Lisans Bedeli",
        "Bulut Depolama Aboneliği",
        "SaaS Hizmet Bedeli",
        "API Kullanim Ücreti",
        "Yillik Lisans Yenileme",
    ],
    HarcamaKategorisi.DANISMANLIK: [
        "Danişmanlik Hizmeti",
        "Hukuki Danişmanlik",
        "Denetim Hizmeti",
        "Proje Danişmanliği",
    ],
    HarcamaKategorisi.ALKOL: [
        "Şarap Servisi",
        "Bira",
        "Kokteyl Servisi",
        "İçki Servisi",
    ],
    HarcamaKategorisi.EGLENCE: [
        "Organizasyon Bedeli",
        "Etkinlik Bileti",
        "Eğlence Hizmeti",
        "Sinema/Tiyatro Bileti",
    ],
    HarcamaKategorisi.TUTUN_URUNLERI: [
        "Sigara", "Puro", "Elektronik Sigara", "Nargile Hizmeti",
    ],
    HarcamaKategorisi.KUMAR: [
        "Piyango Bileti", "Bahis Ödemesi", "Casino Harcamasi",
    ],
    HarcamaKategorisi.DIGER: [
        "Genel Gider",
        "Muhtelif Harcama",
        "Diğer Hizmet Bedeli",
        "Diğer Market Ürünleri",
    ],
    HarcamaKategorisi.KISISEL_BAKIM: [
        "Şampuan",
        "Krem",
        "Makyaj Malzemesi",
        "Diş Macunu",
        "Parfüm",
        "Tıraş Malzemesi",
    ],
    HarcamaKategorisi.TEMIZLIK: [
        "Deterjan",
        "Temizlik Bezi",
        "Süpürge",
        "Temizlik Spreyi",
        "Bulaşık Makinesi Tableti",
    ],

}

# Ürün adı -> birim eşlemesi. Sadece birim bilgisi CSV'de VARSA doldurulur
# (danışmanlık/konaklama/ulaşım gibi el ile hazırlanmış küçük havuzlar).
# rastgele_kalem, bir aciklama seçtikten SONRA burada karşılığı var mı diye
# bakar; yoksa eski davranışa (kategori bazlı rastgele birim) düşer.
URUN_BIRIM_ESLEME: dict[str, str] = {}


def _urun_birim_kayitlarini_isle(
    urunler_by_kategori: dict[HarcamaKategorisi, list[tuple[str, str | None]]]
) -> dict[HarcamaKategorisi, list[str]]:
    """
    (urun_adi, birim) çiftlerinden oluşan yükleyici çıktısını ACIKLAMA_HAVUZU'nun
    beklediği düz isim listesine çevirir; birim bilgisi varsa global
    URUN_BIRIM_ESLEME sözlüğüne kaydeder.
    """
    sonuc: dict[HarcamaKategorisi, list[str]] = {}
    for kategori, kayitlar in urunler_by_kategori.items():
        isimler: list[str] = []
        for urun_adi, birim in kayitlar:
            isimler.append(urun_adi)
            if birim:
                URUN_BIRIM_ESLEME[urun_adi] = birim
        sonuc[kategori] = isimler
    return sonuc

# CSV'lerden gelen ürünlerle havuzu zenginleştir/değiştir (dosya yoksa sabit liste kalır)
_market_urunleri = market_urunleri_yukle()

TEMEL_GIDA_MARKET_HAVUZU = (
    _market_urunleri.get(HarcamaKategorisi.TEMEL_GIDA)
    or ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMEL_GIDA]
)
TEMEL_GIDA_MARKET_AGIRLIGI = 0.6

# YEMEK_HIZMETI: CSV'deki ~100 kusur urun havuzun ONUNE eklenir, sabit
# el-yazimi liste SILINMEZ -- ayni faturada kalem sayisi CSV'yi tuketirse
# (BUYUK_HAVUZ_ESIGI altinda oldugu icin rastgele_kalem'deki mevcut dedup
# filtresi zaten devrede), filtreleme otomatik olarak eski sabit listeye
# duser. Ayri bir agirlik/fallback kodu gerekmez.
_yemek_urunleri = yemek_urunleri_yukle()
if _yemek_urunleri:
    ACIKLAMA_HAVUZU[HarcamaKategorisi.YEMEK_HIZMETI] = (
        _yemek_urunleri + ACIKLAMA_HAVUZU[HarcamaKategorisi.YEMEK_HIZMETI]
    )

# 1. Danışmanlık Ürünleri
_danismanlik_urunleri = _urun_birim_kayitlarini_isle(danismanlik_urunleri_yukle())
for kategori, urunler in _danismanlik_urunleri.items():
    if urunler:
        ACIKLAMA_HAVUZU[kategori] = urunler  # Üzerine yaz (ez)

# 2. Konaklama Ürünleri
_konaklama_urunleri = _urun_birim_kayitlarini_isle(konaklama_urunleri_yukle())
for kategori, urunler in _konaklama_urunleri.items():
    if urunler:
        ACIKLAMA_HAVUZU[kategori] = urunler  # Üzerine yaz (ez)

# 3. Ulaşım Ürünleri
_ulasim_urunleri = _urun_birim_kayitlarini_isle(ulasim_urunleri_yukle())
for kategori, urunler in _ulasim_urunleri.items():
    if urunler:
        # Eğer ulasim.csv içinde kategori HIZMETI ve BIREYSEL diye ayrılmışsa
        # bu döngü direkt doğru yere yazacaktır.
        ACIKLAMA_HAVUZU[kategori] = urunler

_temiz_urunler = temiz_urunleri_yukle()

_anomali_urunleri = anomali_urunleri_yukle()

# Yasakli kalemin SATICI ekseninde makul olup olmadigi (bkz.
# anomali_urun_makullugu_yukle docstring'i). validators.py bunu okur.
ANOMALI_URUN_MAKULLUGU: dict[str, set[str]] = anomali_urun_makullugu_yukle()

# market_urunleri.csv'nin eslenmeyen kategorilerinden geri kazanilanlar
# (SİGARA -> tutun, KAĞIT/EV -> sarf). Bkz. market_ek_kategoriler_yukle.
_market_ek, _market_sigaralari = market_ek_kategoriler_yukle()

# SİGARA kalemleri SATICI EKSENINDE market icin MAKULDUR (§16): markette sigara
# satmak olagandir, sirketin odemesi degildir. Bu kayit olmadan 19 bin kalem
# `yasakli_kategori` ile birlikte `is_kolu_kategori_uyumsuzlugu`'nu DA tetikler
# ve iki etiket yeniden %100 korele hale gelirdi -- §16'da kapatilan hatanin
# aynisi. `bufe`/`tekel` ayri bir IsKolu olmadigi icin karsiligi `market`tir.
for _sigara in _market_sigaralari:
    ANOMALI_URUN_MAKULLUGU.setdefault(_sigara, set()).add(IsKolu.MARKET.value)

# ALKOL/EGLENCE/TUTUN_URUNLERI/KUMAR: elle yazilmis sabit listeler sadece
# 3-4 elemanliydi, CSV çok daha zengin (100'lerce ürün) -- CSV varsa
# üzerine YAZIYORUZ (append degil), yoksa eski sabit liste fallback olarak kalir.
for _kategori in (
    HarcamaKategorisi.ALKOL,
    HarcamaKategorisi.EGLENCE,
    HarcamaKategorisi.TUTUN_URUNLERI,
    HarcamaKategorisi.KUMAR,
):
    _ekstra = _anomali_urunleri.get(_kategori)
    if _ekstra:
        ACIKLAMA_HAVUZU[_kategori] = _ekstra
    # market_urunleri.csv'den gelen SİGARA kalemleri EKLENIR (uzerine yazilmaz):
    # elle yazilmis jenerik adlar ('Sarma Tutun (50gr)', 'Nargile') makullik
    # kolonuyla birlikte kalmali -- onlar market icin makul DEGIL, marka sigarasi
    # ise makul. Ikisi bir arada varyans uretir (§16'nin istedigi de bu).
    _ek_yasakli = _market_ek.get(_kategori)
    if _ek_yasakli:
        ACIKLAMA_HAVUZU[_kategori] = ACIKLAMA_HAVUZU[_kategori] + _ek_yasakli

# CSV'den geleni ata, boş/yoksa ACIKLAMA_HAVUZU'ndaki (artık zenginleştirilmiş)
# mevcut genel açıklama listesine düş -- ayrı, alakasız tek satırlık
# fallback listelerine artık gerek yok.
for _kategori in (HarcamaKategorisi.KISISEL_BAKIM,):
    ACIKLAMA_HAVUZU[_kategori] = _temiz_urunler.get(_kategori) or ACIKLAMA_HAVUZU[_kategori]

# KAĞIT -> TEMIZLIK, EV -> OFIS_SARF_MALZEME (beyaz liste suzgeciyle geri
# kazanilan kurumsal sarf malzemesi; bkz. market_ek_kategoriler_yukle). EKLENIR.
# ofis_sarf_malzeme havuzu bu eklemeden ONCE 163 kalemdi ve dijital kartvizit
# varyantlari baskindi -- bir kirtasiye fisinin dort kalemi de kartvizit
# cikiyordu (olculdu). Gercek sarf malzemesi girmesi onu dengeler.
for _kategori in (HarcamaKategorisi.TEMIZLIK, HarcamaKategorisi.OFIS_SARF_MALZEME):
    _ek_sarf = _market_ek.get(_kategori)
    if _ek_sarf:
        ACIKLAMA_HAVUZU[_kategori] = ACIKLAMA_HAVUZU[_kategori] + _ek_sarf

# Market CSV'sindeki KOZMETİK/DETERJAN TEMİZLİK satırlarıyla ilgili
# havuzları zenginleştir (üzerine yazma, ekle).
_market_kisisel_bakim = _market_urunleri.get(HarcamaKategorisi.KISISEL_BAKIM, [])
if _market_kisisel_bakim:
    ACIKLAMA_HAVUZU[HarcamaKategorisi.KISISEL_BAKIM] = (
        ACIKLAMA_HAVUZU[HarcamaKategorisi.KISISEL_BAKIM] + _market_kisisel_bakim
    )

_market_temizlik = _market_urunleri.get(HarcamaKategorisi.TEMIZLIK, [])
if _market_temizlik:
    ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] = (
        ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] + _market_temizlik
    )

# TEMIZLIK: artik ayri IsKolu yok, MARKET altinda kullaniliyor. CSV'den
# geleni ata, yoksa tek satirlik fallback.
ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK] = _temiz_urunler.get(HarcamaKategorisi.TEMIZLIK) or ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMIZLIK]

# Supermarket etiketiyle gelen TEMEL_GIDA urunleri (temiz_urunler.csv) —
# 2026-07-30'da DEVRE DISI BIRAKILDI (bos liste).
#
# Havuz 1.753 satirdi, market_urunleri.csv'nin 500.438'i yaninda %0,35 — AMA
# rastgele_kalem icinde %40 AGIRLIKLA cekiliyordu (TEMEL_GIDA_MARKET_AGIRLIGI).
# Yani fisteki gida kalemlerinin ~%40'i bu kucuk havuzdan geliyordu ve icerigi
# kurumsal bir markette bulunmayacak B2C artiklariydi: 'Petimix Dana Girtlak
# Cigneme Kemikleri' (kopek maması), 'Fare Kovucu Ultrasonic', 'Bambala Pancar
# Corbasi +6 Ay' (bebek mamasi), 'Tupperware Shaker', 'Masaj Yastigi Aleti'.
# Faz B'de "Babyjem bebek seti" tipi aciklamalarin kaynagi buydu (§17, kalitenin
# tavani fis gercekciligi).
#
# market_urunleri.csv gercek bir market listesi (Havuc, Sutas Yayik Tereyag,
# Sivri Biber, Coca Cola) ve TEMIZLIK'i de kapsiyor (22.748) -> kurumsal filtreye
# ihtiyaci yok, tek basina yeterli. Cesitlilik gerekcesi de gecerliligini
# yitiriyor: 500 bin satirlik havuzda cesitlilik sorunu yok.
#
# GERI ALMAK ISTERSEN: temiz_urunler.csv'nin Supermarket satirlarini once
# urun_kurumsal_filtre.py'den gecir, yoksa ayni artiklar %40 agirlikla geri gelir.
TEMEL_GIDA_SUPERMARKET_HAVUZU: list[str] = []

# ACIKLAMA_HAVUZU[TEMEL_GIDA]: sadece uzunluk hesaplari (rastgele_fatura
# icindeki toplam_musait_aciklama) icin birlesik liste olarak tutuluyor;
# asil SECIM rastgele_kalem icinde iki ayri havuzdan agirlikli yapiliyor.
ACIKLAMA_HAVUZU[HarcamaKategorisi.TEMEL_GIDA] = TEMEL_GIDA_MARKET_HAVUZU + TEMEL_GIDA_SUPERMARKET_HAVUZU

# Elle yazılmış havuzu ZATEN OLAN kategorilerde CSV'den geleni ÜZERİNE
# YAZMA, mevcut listeye EKLE.
for _kategori in (
    HarcamaKategorisi.OFIS_MOBILYA,
    HarcamaKategorisi.OFIS_SARF_MALZEME,
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    HarcamaKategorisi.YAZILIM_LISANS,
):
    _ekstra = _temiz_urunler.get(_kategori)
    if _ekstra:
        ACIKLAMA_HAVUZU[_kategori] = ACIKLAMA_HAVUZU[_kategori] + _ekstra

# Kategoriye göre makul birim fiyat araliği (KDV hariç, TL)
# (kategori, birim) -> (min, max) fiyat araliği
FIYAT_ARALIGI_DETAYLI = {
    (HarcamaKategorisi.EGLENCE, "Kişi"): (300, 4000),
    (HarcamaKategorisi.EGLENCE, "Adet"): (500, 15000),

    (HarcamaKategorisi.YEMEK_HIZMETI, "Kişi"): (200, 2000),
    (HarcamaKategorisi.YEMEK_HIZMETI, "Adet"): (100, 1000),

    (HarcamaKategorisi.KONAKLAMA, "Gece"): (1500, 15000),
    (HarcamaKategorisi.KONAKLAMA, "Adet"): (1500, 15000),

    (HarcamaKategorisi.TEMEL_GIDA, "Kg"): (50, 800), # Sebzeden kirmizi ete uzanan aralik
    (HarcamaKategorisi.TEMEL_GIDA, "Litre"): (10, 800), # Su/Sütten Zeytinyağina uzanan aralik
    (HarcamaKategorisi.TEMEL_GIDA, "Adet"): (20, 800),

    (HarcamaKategorisi.ULASIM_BIREYSEL, "Km"): (150, 1750),
    (HarcamaKategorisi.ULASIM_HIZMETI, "Km"): (250, 3500),
    (HarcamaKategorisi.ULASIM_HIZMETI, "Ton"): (1000, 35000),
    # 2026-08-01: bu ikisi CSV'de (ulasim_urunleri.csv) BIRIM olarak zaten
    # yaziliydi ama fiyat katmani YOKTU -> sessizce FIYAT_ARALIGI_GENEL'e
    # (500, 20000) dusuyorlardi; aylik antrepo ile saatlik hamaliye ayni
    # bandi paylasiyordu. Guvenli bant (50, 100000).
    (HarcamaKategorisi.ULASIM_HIZMETI, "Ay"): (5000, 60000),    # antrepo/depolama
    (HarcamaKategorisi.ULASIM_HIZMETI, "Saat"): (500, 3000),    # yukleme/bosaltma
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Gün"): (800, 3000),
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Litre"): (25, 70),   # akaryakit litre fiyati
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Saat"): (100, 1000),
    # Tavan 4000: uretim tavani = 4000 x FIYAT_TASMA_ORANI = 4800 < politika
    # limiti 5000. 4500 yazildiginda tavan 5400 cikip sahte limit_asimi ureti[yor]du.
    (HarcamaKategorisi.ULASIM_BIREYSEL, "Ay"): (1500, 4000),   # otopark aylik abonelik
    (HarcamaKategorisi.KONAKLAMA, "Saat"): (300, 3000),

    (HarcamaKategorisi.OFIS_SARF_MALZEME, "Adet"): (50, 500),
    (HarcamaKategorisi.OFIS_SARF_MALZEME, "Kutu"): (300, 2500),
    (HarcamaKategorisi.OFIS_MOBILYA, "Adet"): (2000, 20000),
    (HarcamaKategorisi.TEKNOLOJI_EKIPMAN, "Adet"): (3000, 40000),   # bilgisayar/sunucu daha pahali olabilir
    

    (HarcamaKategorisi.DANISMANLIK, "Saat"): (1000, 5000),
    (HarcamaKategorisi.DANISMANLIK, "Ay"): (10000, 100000),
    (HarcamaKategorisi.DANISMANLIK, "Adet"): (2000, 50000),

    (HarcamaKategorisi.YAZILIM_LISANS, "Ay"): (300, 5000),
    (HarcamaKategorisi.YAZILIM_LISANS, "Kullanici"): (1000, 25000),
    (HarcamaKategorisi.YAZILIM_LISANS, "Adet"): (500, 15000),
    
}

# (kategori, birim) sözlükte yoksa geri düşülecek genel aralik (fallback)
FIYAT_ARALIGI_GENEL = {
    HarcamaKategorisi.YEMEK_HIZMETI: (150, 2000),
    HarcamaKategorisi.TEMEL_GIDA: (20, 1000),
    HarcamaKategorisi.ULASIM_HIZMETI: (500, 20000),    # B2B nakliye/kargo genelde daha yüksek tutarli
    HarcamaKategorisi.ULASIM_BIREYSEL: (30, 3000),      # taksi/yakit/otopark daha küçük tutarli
    HarcamaKategorisi.KONAKLAMA: (1500, 15000),
    HarcamaKategorisi.OFIS_SARF_MALZEME: (50, 2500),   # kirtasiye + sarf paket birleşik fallback
    HarcamaKategorisi.OFIS_MOBILYA: (2000, 20000),        # masa/sandalye/kahve makinesi
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: (3000, 40000),    # bilgisayar/sunucu daha pahali olabilir
    HarcamaKategorisi.YAZILIM_LISANS: (300, 25000),
    HarcamaKategorisi.DANISMANLIK: (2000, 100000),
    HarcamaKategorisi.ALKOL: (150, 4000),
    HarcamaKategorisi.EGLENCE: (300, 15000),
    HarcamaKategorisi.TUTUN_URUNLERI: (50, 500),
    HarcamaKategorisi.KUMAR: (100, 5000),
    HarcamaKategorisi.DIGER: (100, 5000),
    HarcamaKategorisi.DIGER: (100, 5000),
    HarcamaKategorisi.KISISEL_BAKIM: (50, 800),
    HarcamaKategorisi.TEMIZLIK: (30, 500),
}

# Kategoriye göre uygun birim
BIRIM_HAVUZU = {
    HarcamaKategorisi.YEMEK_HIZMETI: ["Adet", "Kişi"],
    HarcamaKategorisi.TEMEL_GIDA: ["Kg", "Adet", "Litre"],
    # 'Kg' 2026-08-01'de ÇIKARILDI: miktar araliğimiz 0,5-10 olduğu için
    # "3,4 Kg parsiyel nakliye" saçmaydi; parsiyel yükün ölçeği TON.
    # 'Ay'/'Saat' EKLENDİ: CSV'de (ulasim_urunleri.csv) zaten kullaniliyordu
    # -- aylik antrepo, saatlik yükleme/boşaltma. Havuzda olmadiklari için
    # fiyat katmanlari da yoktu (bkz. FIYAT_ARALIGI_DETAYLI notu).
    HarcamaKategorisi.ULASIM_HIZMETI: ["Adet", "Km", "Ton", "Ay", "Saat"],
    # 'Saat' ikisine de 2026-08-01'de eklendi: CSV zaten kullaniyordu ('Otopark
    # Ucreti', 'Otel Toplanti Odasi Kiralama') ve fiyat katmanlari da tanimliydi,
    # yalniz havuz listesi geride kalmisti.
    HarcamaKategorisi.ULASIM_BIREYSEL: ["Adet", "Km", "Litre", "Gün", "Saat", "Ay"],
    HarcamaKategorisi.KONAKLAMA: ["Gece", "Adet", "Saat"],
    HarcamaKategorisi.OFIS_SARF_MALZEME: ["Adet", "Kutu"],
    HarcamaKategorisi.OFIS_MOBILYA: ["Adet"],
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: ["Adet"],
    HarcamaKategorisi.YAZILIM_LISANS: ["Ay", "Kullanici", "Adet"],
    HarcamaKategorisi.DANISMANLIK: ["Saat", "Ay", "Adet"],
    HarcamaKategorisi.ALKOL: ["Adet", "Şişe"],
    HarcamaKategorisi.EGLENCE: ["Adet", "Kişi"],
    HarcamaKategorisi.TUTUN_URUNLERI: ["Adet", "Paket"],
    HarcamaKategorisi.KUMAR: ["Adet"],
    HarcamaKategorisi.DIGER: ["Adet"],
    HarcamaKategorisi.KISISEL_BAKIM: ["Adet"],
    HarcamaKategorisi.TEMIZLIK: ["Adet"],
}
# İş kolu bazlı SEÇİM ağırlığı: her iş kolunun izinli kategorilerindeki
# toplam açıklama/ürün sayısına göre (log ölçekli) hesaplanır. Amaç:
# market/teknoloji gibi CSV'den binlerce ürün çekebilen iş kollarının
# faturalarda AŞIRI baskın olmasını engellerken, danışmanlık/konaklama/
# ulaşım gibi havuzu çok küçük (20-40 ürün) iş kollarının da GERÇEKÇİ
# şekilde daha az üretilmesini sağlamak -- küçük havuzda çok sayıda fatura
# üretilirse ayni açıklamalar defalarca tekrar etmek zorunda kalıyor.
# log1p kullanılıyor çünkü ham havuz büyüklükleri arasında (23 ile 500.000+
# arası) devasa fark var; düz orantı diğer TÜM iş kollarını yok sayardı.
# TABAN_AGIRLIK, en küçük havuzlu iş kolunun bile sıfıra yakın olasılığa
# düşmemesini garantiler.
TABAN_AGIRLIK = 1.0

# GERCEKLIK CARPANI: havuz boyutu "bu sektor gercek masrafta ne siklikta gorulur"
# sorusunun cevabi DEGIL, yalnizca hangi CSV'yi bulabildigimizin izi. Gercek fis
# etiketlemesinde (30 fis) yemek, yol ve otopark baskin cikti; carpan o gozlemi
# tasir. Havuz boyutu sirasini BOZMAZ (asagidaki monotonluk zorlamasi).
# Bir is kolunun agirligini ancak havuzu bu KAT kadar buyuk olan kisitlar.
# 2.0 = "en az iki kati". Yakin havuzlar (30 vs 35) birbirini etkilemez.
MONOTONLUK_ORANI = 2.0

IS_KOLU_GERCEKLIK_CARPANI: dict[IsKolu, float] = {
    IsKolu.RESTORAN: 2.0,
    IsKolu.ULASIM_SAGLAYICI: 1.8,
    IsKolu.OTEL: 1.3,
    IsKolu.MARKET: 1.0,
    IsKolu.OFIS_TEDARIK: 1.0,
    IsKolu.ORGANIZASYON: 0.9,
    IsKolu.TEKNOLOJI: 0.8,
    IsKolu.DANISMANLIK_FIRMASI: 0.6,
    IsKolu.LOJISTIK_FIRMASI: 0.6,
}


def _is_kolu_agirliklarini_hesapla() -> dict[IsKolu, float]:
    """log(BENZERSIZ havuz) x gerceklik carpani, ardindan MONOTONLUK zorlamasi.

    BENZERSIZ: havuz tekrar iceriyor (market_urunleri'nde EKMEK 9.122 kez) ve o
    tekrar 'ekmek sik satilir' demek, 'market fisi cok olmali' demek DEGIL. Ham
    satir sayisi okundugunda iki eksen birbirine karisiyordu.

    MONOTONLUK: havuzu kucuk bir is kolu, havuzu buyuk olanin agirligini GECEMEZ.
    Aksi halde 23 kalemlik ulasim havuzu 7.941 kalemlik marketten fazla fatura
    uretir ve ayni kalemler defalarca tekrar eder. Carpan bu tavanin ALTINDA
    serbesttir; tavan bagladiginda gerceklik hedefi kategori duzeyinde yine
    tutuyor, cunku yemek uc ayri is kolundan geliyor (restoran+otel+organizasyon).
    """
    ham: dict[IsKolu, float] = {}
    havuz_boyu: dict[IsKolu, int] = {}
    for is_kolu in IsKolu:
        izinli_kategoriler = IS_KOLU_KATEGORILERI.get(is_kolu, [])
        benzersiz = sum(len(set(ACIKLAMA_HAVUZU.get(k, []))) for k in izinli_kategoriler)
        havuz_boyu[is_kolu] = benzersiz
        carpan = IS_KOLU_GERCEKLIK_CARPANI.get(is_kolu, 1.0)
        ham[is_kolu] = (TABAN_AGIRLIK + math.log1p(benzersiz)) * carpan

    # YAKIN HAVUZLAR BIRBIRINI KISITLAMAZ. Kisit yalniz havuzu MONOTONLUK_ORANI
    # katindan buyuk olan is kollarindan gelir. Zincirli surumde 30 kalemlik ulasim
    # ile 35 kalemlik danismanlik birbirini kisitliyordu: danismanliga 12 kalem
    # eklenince ulasim onun ALTINA dustu, onun DUSUK carpanini (0,6) miras aldi ve
    # payi %10,4'ten %5,4'e indi. Bes kalemlik bir fark, ilgisiz bir is kolunun
    # payini yariya indiriyordu (olculdu).
    agirliklar: dict[IsKolu, float] = {}
    for is_kolu, boyut in havuz_boyu.items():
        kisitlayanlar = [ham[j] for j, b in havuz_boyu.items()
                         if b >= boyut * MONOTONLUK_ORANI]
        agirliklar[is_kolu] = min([ham[is_kolu]] + kisitlayanlar)
    return agirliklar


IS_KOLU_AGIRLIKLARI = _is_kolu_agirliklarini_hesapla()


"""
SEKTOR_KELIME_HAVUZU = {
    HarcamaKategorisi.TEMEL_GIDA: ["Bereket", "Öz", "Tarim", "Gida Pazarlama", "Market"],
    HarcamaKategorisi.YEMEK_HIZMETI: ["Lezzet", "Sofra", "Mutfak", "Catering"],
    HarcamaKategorisi.ULASIM: ["Hizli", "Güven", "Transfer", "Lojistik"],
    HarcamaKategorisi.KONAKLAMA: ["Grand", "Otel", "Konaklama", "Suite"],
    HarcamaKategorisi.OFIS_SARF_MALZEME: ["Kirtasiye", "Ofis Sarf", "Büro Malzemeleri"],
    HarcamaKategorisi.OFIS_DEMIRBAS: ["Mobilya", "Ofis Ekipmanlari", "Demirbaş"],
    HarcamaKategorisi.YAZILIM_LISANS: ["Yazilim", "Teknoloji", "Bilişim", "Tech"],
    HarcamaKategorisi.DANISMANLIK: ["Danişmanlik", "Consulting", "Denetim"],
    HarcamaKategorisi.ALKOL: ["İçki Pazarlama", "Şarap Evi", "Meyhane"],
    HarcamaKategorisi.EGLENCE: ["Organizasyon", "Etkinlik", "Prodüksiyon"],
    HarcamaKategorisi.DIGER: ["Ticaret", "Genel", "Pazarlama"],
}
"""

NITELIK_KELIME_HAVUZU = [
    "Modern", "Global", "Anadolu", "Ege", "Merkez", "Yıldız",
    "Başkent", "Marmara", "Kardeş", "Öncü", "Doğa", "Vizyon",
    "Akdeniz", "Karadeniz", "Toros", "Zirve", "Ufuk", "Bereket",
    # 2026-07-30 yazim düzeltmesi: Yildiz->Yıldız, Bariş->Barış, Bati->Batı ve
    # Sinir->SINIR. Son dördü yalnız diakritik değil ANLAM hatasıydı: "sinir"
    # (nerve) yerine "sınır" (border) -- lojistik firmasında kastedilen budur.
    "Değer", "Fener", "Umut", "Barış", "Güven", "Sınır",
    "Doruk", "Ata", "Yeni", "Batı", "Doğu", "Kuzey",
]


IS_KOLU_SEKTOR_KELIME = {
    IsKolu.RESTORAN: ["Sofra", "Lezzet", "Mutfak"],
    IsKolu.MARKET: ["Market", "Gıda Pazarlama", "Tarım"],
    IsKolu.OTEL: ["Grand", "Otel", "Resort"],
    IsKolu.OFIS_TEDARIK: ["Kırtasiye", "Ofis Sarf", "Büro Malzemeleri"],
    IsKolu.TEKNOLOJI: ["Yazılım", "Teknoloji", "Bilişim"],
    IsKolu.DANISMANLIK_FIRMASI: ["Danışmanlık", "Consulting", "Denetim"],
    IsKolu.LOJISTIK_FIRMASI: ["Lojistik", "Nakliyat", "Kargo"],
    # Ulaşım sektör kelimeleri hizmete ÖZEL (Taksi/Akaryakit/Rent A Car) ya da
    # JENERİK (Turizm/Seyahat/Ulaşım). Özel olanlar yalnız fişte ilgili kalem varsa
    # firma adına girer (bkz. SEKTOR_KELIME_KALEM_KOSULU); jenerikler her zaman uygun.
    IsKolu.ULASIM_SAGLAYICI: ["Taksi", "Otogar", "Akaryakıt", "Rent A Car",
                              "Turizm", "Seyahat", "Ulaşım"],
    IsKolu.ORGANIZASYON: ["Organizasyon", "Etkinlik", "Prodüksiyon"],
}

# Bir sektör kelimesi firma adına ancak fişteki kalemlerden biri ilgili anahtar
# kelimeyi içeriyorsa girebilir (ör. 'Taksi' yalnız faturada taksi kalemi varsa).
# Burada OLMAYAN sektör kelimeleri jeneriktir, her zaman uygundur.
SEKTOR_KELIME_KALEM_KOSULU = {
    "Taksi": ("taksi",),
    # ANAHTAR, IS_KOLU_SEKTOR_KELIME'deki yazımla BİREBİR aynı olmalı (sözlük
    # lookup'ı diakritiğe duyarlı). 2026-07-30'da "Akaryakit" -> "Akaryakıt"
    # düzeltildi; anahtar da birlikte değişti, yoksa koşul sessizce devre dışı
    # kalıp yakıt kelimesi ilgisiz firmalara girerdi.
    "Akaryakıt": ("akaryakit", "yakit", "benzin", "motorin"),
    "Rent A Car": ("arac kiralama", "rent a car", "kiralama"),
    "Otogar": ("otobus", "otogar", "sehirlerarasi"),
}


def _ascii_kucuk(metin: str) -> str:
    """Türkçe metni ASCII küçük harfe indirger (kalem adı ile anahtar kelime
    eşleşmesini diakritik farkına takılmadan yapmak için)."""
    metin = metin.replace("İ", "i").replace("I", "ı").lower()
    return metin.translate(str.maketrans("ğüşıöç", "gusioc"))


# --- Mutfak uyumu -----------------------------------------------------------
# OSM'den gelen restoran adlarinin %44'u mutfak kelimesi tasiyor ("Tatlises Cig
# Kofte", "Duru Balik"). Ad dikkate alinmadan kalem secilirse cigkoftecidan
# sushi cikiyor; bu etiketleri bozmaz ama aciklama uretiminde ("X'ten aldik")
# tutarsiz metin dogurur. Burada firma adindan mutfak tespit edilip kalem havuzu
# ilgili menu bolumleriyle sinirlanir.
#
# NOT: Bu, emekliye ayrilan sektor-kelime / is_kolu-geri-okuma mekanizmasinin
# geri donusu DEGILDIR. O, ADDAN is_kolu CIKARMAYA calisiyordu; bu ise zaten
# bilinen is_kolu icinde yemek secimini daraltir.
#
# SIRA ONEMLI, ilk eslesen kazanir: "Tatlises Cig Kofte" hem "tatli" hem
# "cig kofte" iceriyor -- cigkofte once geldigi icin dogru siniflanir.
# Dar bolume ek olarak ilgili GENEL bolum de verilir: burgerci fast_food'daki
# klasik burgerleri, pizzaci hamur_isleri'ndeki pizzalari da satabilsin.
# Desenler DAR tutulur: "asya"/"deniz" gibi tek basina yaygin Turkce isimler
# (Asya Kasap, Deniz Restaurant) yanlis pozitif uretip kasaba sushi yazdiriyordu.
# Kural: kelime ancak mutfagi TEK BASINA belirtiyorsa desende yer alir.
MUTFAK_KISITLARI: list[tuple[str, set[str]]] = [
    (r"cig ?kofte",                        {"cigkofte", "icecekler"}),
    # Kokoreç/midyeci sokak lezzeti tarafı; balıkçıdan ÖNCE gelmeli, yoksa
    # 'Midyeci ...' balık menüsüne düşer (ölçüldü: fast_food kovasında 42 kokoreç).
    (r"kokorec|midyeci",                   {"kokorec", "icecekler"}),
    (r"borek|boreg|borekci",               {"borek", "icecekler"}),
    (r"sushi|susi|wok|japon|ramen|noodle|teriyaki|uzak ?dogu|asya mutfa|cin mutfa|cin lokanta",
                                           {"uzakdogu", "icecekler"}),
    (r"pastane|tatlici|baklava|dondurma|waffle|kahve|coffee|cafe|kafe|patisserie",
                                           {"pastane_tatli", "tatlilar", "tost_sandvic", "icecekler"}),
    (r"balik|deniz urunleri|deniz mahsul", {"balik", "ara_sicaklar_mezeler", "corbalar", "icecekler"}),
    (r"pizza",                             {"pizza", "icecekler", "ara_sicaklar_mezeler"}),
    (r"pide|lahmacun|etli ekmek",          {"pide_lahmacun", "corbalar", "icecekler"}),
    # Fırın/unlu mamul. Pide'den SONRA gelmeli: 'Taş Fırın Pide' bir pidecidir
    # (OSM bu adı `shop=bakery` sanıp fırın demişti, 2026-08-13 dersi).
    (r"\bfirin|halk ekmek|unlu mamul|simit|pogaca",
                                           {"borek", "pastane_tatli", "icecekler"}),
    (r"burger",                            {"burger", "icecekler"}),
    # Dönerci/kebapçı geniş menülüdür: kendi bölümü + et yemekleri + pide.
    (r"doner|kebap|kebab|kofteci|ocakbasi|durumcu",
                                           {"doner_kebap", "ana_yemekler_et", "pide_lahmacun",
                                            "corbalar", "icecekler"}),
    (r"corbaci|kelle ?paca|iskembeci",     {"corbalar", "icecekler"}),
]

YEMEK_MENU_BOLUMLERI: dict[str, list[str]] = yemek_urunleri_bolumlu_yukle()


# ---------------------------------------------------------------------------
# FIRMA ADI KISITI -- MUTFAK_KISITLARI'nin restoran DISI is kollarina genellemesi
# (2026-07-30). Problem ayni: firma adi is_kolu'ndan DAHA DAR bir urun yelpazesi
# isaret ediyor, ama kalem secimi ada bakmiyordu. Olculen sonuclar (Faz B pilotu):
# 'Kuruyemis'ten chia tohumu + camasir yumusaticisi', 'Kirtasiye'den ofis masasi'.
#
# MUTFAK_KISITLARI'ndan YAPISAL FARK: restoran CSV'sinde menu BOLUMU kolonu var,
# daraltma bolum secerek yapiliyor. Market/ofis/teknoloji havuzlari duz urun adi
# listesi -> daraltma iki ayri kaldiracla yapilir:
#   (1) KATEGORI daralt  : kirtasiyeci OFIS_MOBILYA satmaz  -> izinli kategori kumesi
#   (2) URUN ADI daralt  : kuruyemisci her gidayi satmaz    -> urun adi deseni
# Ucuncu eleman None ise yalniz (1) uygulanir.
#
# SIRA ONEMLI: ilk eslesen kazanir (MUTFAK_KISITLARI ile ayni sozlesme).
#
# `tekel` NOTU: gercek bir tekel alkol/tutun satar ama bunlar POLICY_YASAKLI
# kategorilerdir ve MARKET'in izinli kategorilerinde YOKTUR. Temiz uretimde
# alkol/sigara CIKARMAK anomali uretmek olurdu (§12: kisit yalniz TEMIZ uretime
# girer, anomaliyi enjektorler uretir). Bu yuzden tekel, bufe gibi ele alinir:
# icecek + paketli atistirmalik.
FIRMA_ADI_KISITLARI: dict[IsKolu, list[tuple[str, set[HarcamaKategorisi], str | None]]] = {
    IsKolu.MARKET: [
        (r"kuruyemis|kuru yemis|cerezci|cerez",
         {HarcamaKategorisi.TEMEL_GIDA},
         r"f[ıi]st[ıi]k|badem|ceviz|f[ıi]nd[ıi]k|leblebi|kaju|antep|kuruyemis"
         r"|kuru ?[uü]z[uü]m|kay[ıi]s[ıi]|incir|[cç]ekirdek|kavrulmu|cerez|[cç]erez"),
        (r"sarkuteri|sarkuteri|kasap|et ?urunleri",
         {HarcamaKategorisi.TEMEL_GIDA},
         r"salam|sucuk|sosis|past[ıi]rma|kavurma|jambon|[sş]ar[kc]uteri|ka[sş]ar"
         r"|peynir|terey[aa][gğ]|kiyma|k[ıi]yma|but|kanat|bonfile|dana|kuzu|tavuk"),
        (r"bufe|tekel|kiosk",
         {HarcamaKategorisi.TEMEL_GIDA},
         r"gazoz|kola|cola|soda|ayran|maden suyu|cips|[cç]ikolata|sak[ıi]z|gofret"
         r"|kraker|bisk[uü]vi|enerji i[cç]ece|meyve suyu|ice tea|kahve|[cç]ay"),
    ],
    IsKolu.OFIS_TEDARIK: [
        # Mobilyaci kirtasiye satmaz, kirtasiyeci ofis masasi satmaz. En temiz
        # ayrim burada (osm adlarinin %41'i bu iki kelimeden birini tasiyor).
        (r"mobilya|koltuk|masa ?sandalye", {HarcamaKategorisi.OFIS_MOBILYA}, None),
        (r"kirtasiye|kitap|copy|fotokopi",
         {HarcamaKategorisi.OFIS_SARF_MALZEME}, None),
    ],
    IsKolu.TEKNOLOJI: [
        (r"bilgisayar|computer|elektronik|teknoloji marketi",
         {HarcamaKategorisi.TEKNOLOJI_EKIPMAN}, None),
        (r"bilisim|yazilim|software|dijital",
         {HarcamaKategorisi.YAZILIM_LISANS, HarcamaKategorisi.TEKNOLOJI_EKIPMAN}, None),
    ],
}

# Urun adi daraltmasi sonucu havuz bundan kucuk kalirsa daraltma UYGULANMAZ
# (kategori kisiti yine gecerlidir). Gerekce: 6 urunluk bir alt havuz o firmalarin
# fislerini birbirinin kopyasi yapar ve firma adini kalemden TAHMIN EDILEBILIR
# kilar -- ezberlenebilir kisayol, yani leakage. Olculdu: giyim tarafinda esarp 6,
# triko 13 urun; market tarafinda alt havuzlar 25-75 bin, sorunsuz.
ASGARI_ALT_HAVUZ = 50


# --- KUCUK HAVUZLAR ICIN NEGATIF KISIT (2026-08-01) -------------------------
#
# FIRMA_ADI_KISITLARI POZITIF calisir (izinli urun deseni) ve yukaridaki 50
# esigine tabidir. `ulasim_bireysel` havuzu TOPLAM 23 urun -> esik ASLA gecilemez,
# yani pozitif kisit orada YAPISAL OLARAK devre disidir. Sonuc olculdu: 25k'da
# ulasim_saglayici faturalarinin %14,4'u celiskili --
#   'Akdora Turk Oto Kiralama' -> Yurtici Ucus Bileti
#   'OPET' / 'Petrol Ofisi'     -> Ucak Bileti   (76 fatura)
#   'Modern Seyahat'            -> Vale Hizmeti
# Aciklama uretimi bunu duzeltemez; model imkansiz fisi sadakatle anlatir
# ("OPET'ten ucus bileti aldim").
#
# COZUM POZITIF DEGIL NEGATIF: imkansiz olani ELE, kalani birak. Boylece havuz
# 4 urune inip fisleri birbirinin kopyasi YAPMAZ (ASGARI_ALT_HAVUZ'un korudugu
# sey tam olarak buydu) ama absurtluk de uretilmez. Eleme sonrasi havuz
# ASGARI_NEGATIF_HAVUZ'un altina duserse eleme uygulanmaz.
FIRMA_ADI_YASAK_URUN: dict[IsKolu, list[tuple[str, str]]] = {
    IsKolu.ULASIM_SAGLAYICI: [
        # Akaryakit istasyonu ucak/otobus/feribot bileti ya da arac servisi SATMAZ.
        (r"opet|petrol|shell|\bbp\b|\btotal\b|akaryakit|benzin|istasyon",
         r"bilet|ucus|transfer|arac kiralama|lastik|arac bakim|arac yikama"
         r"|taksi|otopark|vale|muayene|marmaray"),
        # Oto kiralama / otomotiv servisi bilet satmaz. DESEN GENIS TUTULMALI:
        # ilk surum 'oto kiralama|oto servis' yaziyordu ve 'Eren Oto Elektrik',
        # 'Pasha oto emlak', 'Arac Kiralamak' gibi adlari KACIRDI (120k'da 11 kayit).
        # Ayri kelime olarak gecen '\boto\b' ve cekimli 'kiralama[k]' eklendi;
        # 'otobus' bilerek TUTMAZ (kelime siniri yok).
        (r"\boto\b|oto ?elektrik|oto ?emlak|otomotiv|oto servis|rent a car"
         r"|arac kiralama|kiralamak|kiralama|lastik|garaj|filo|servisi",
         r"bilet|ucus|feribot|istanbulkart|metro|tramvay|marmaray|vapur|taksi|otopark"
         r"akaryakit|lpg|benzin|motorin|dizel|diesel|otogaz|adblue|kursunsuz"),
        # Seyahat acentesi arac bakimi/lastik/akaryakit satmaz.
        (r"seyahat|turizm|acenta|acente|hava ?yollari|tur\b",
         r"lastik|arac bakim|arac yikama|otopark|vale|muayene|sarj"
         r"akaryakit|lpg|benzin|motorin|dizel|diesel|otogaz|adblue|kursunsuz"),
        # Taksi duragi bilet ya da akaryakit satmaz.
        (r"taksi|duragi|durak",
         r"bilet|ucus|feribot|lastik|arac bakim|istanbulkart"
         r"|otopark|vale|kiralama|muayene|sarj|marmaray|vapur"
         r"akaryakit|lpg|benzin|motorin|dizel|diesel|otogaz|adblue|kursunsuz"),
    ],
}
ASGARI_NEGATIF_HAVUZ = 3


def firma_yasak_urun_havuzlari(
    is_kolu: IsKolu, satici_unvan: str
) -> dict[HarcamaKategorisi, list[str]]:
    """Firma adina gore IMKANSIZ urunleri elenmis havuzlar. Eslesme yoksa {}.

    POZITIF kisitin (firma_adi_kisiti_sec) tamamlayicisi: o, buyuk havuzlarda
    'yalniz sunlari sat' der; bu, kucuk havuzlarda 'sunlari satma' der."""
    kurallar = FIRMA_ADI_YASAK_URUN.get(is_kolu)
    if not kurallar:
        return {}
    ad = _ascii_kucuk(satici_unvan)
    yasak_desen = next((yd for d, yd in kurallar if re.search(d, ad)), None)
    if yasak_desen is None:
        return {}
    r = re.compile(yasak_desen, re.IGNORECASE)
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    for kat in IS_KOLU_KATEGORILERI.get(is_kolu, []):
        kalan = [u for u in ACIKLAMA_HAVUZU[kat] if not r.search(_ascii_kucuk(u))]
        if ASGARI_NEGATIF_HAVUZ <= len(kalan) < len(ACIKLAMA_HAVUZU[kat]):
            havuzlar[kat] = kalan
    return havuzlar


# Dar havuz ONBELLEGI (is_kolu, desen) -> (izinli kategoriler, dar havuzlar).
# ZORUNLU, kozmetik degil: onbelleksiz surumde her eslesen faturada dar havuz
# SIFIRDAN hesaplaniyordu ve TEMEL_GIDA havuzu 500.438 kalem -- 120k uretimde
# ~3.000 kisitli market faturasi x 500k regex = ~1,5 milyar islem, uretim
# gozle gorulur sekilde yavasladi. Havuzlar ve desenler modul yuklendikten
# sonra SABIT oldugu icin sonuc deterministik, onbellege alinmasi guvenli.
_KISIT_ONBELLEK: dict[tuple[IsKolu, str], tuple[list[HarcamaKategorisi], dict[HarcamaKategorisi, list[str]]] | None] = {}


def firma_adi_kisiti_sec(
    is_kolu: IsKolu, satici_unvan: str
) -> tuple[list[HarcamaKategorisi], dict[HarcamaKategorisi, list[str]]] | None:
    """Firma adi is_kolu'ndan daha dar bir yelpaze isaret ediyorsa (izinli
    kategoriler, daraltilmis havuzlar) ikilisini doner; eslesme yoksa None
    (kisit uygulanmaz, mevcut davranis korunur).

    Ad -> DESEN eslesmesi her cagrida yapilir (ucuz, en fazla 3 regex); desen ->
    dar havuz hesabi ONBELLEKTEN gelir (bkz. _KISIT_ONBELLEK)."""
    # DIKKAT: `firma_kisit_anahtari` BILESIK anahtar doner (pozitif||negatif) ve
    # ciftleme icindir; buradaki POZITIF desen ayrica hesaplanir.
    ad = _ascii_kucuk(satici_unvan)
    eslesme = next(
        ((d, kat, ud) for d, kat, ud in FIRMA_ADI_KISITLARI.get(is_kolu) or ()
         if re.search(d, ad)), None
    )
    if eslesme is None:
        return None
    anahtar_desen, kategoriler, urun_deseni = eslesme
    onbellek_anahtari = (is_kolu, anahtar_desen)
    if onbellek_anahtari in _KISIT_ONBELLEK:
        return _KISIT_ONBELLEK[onbellek_anahtari]
    izinli = [k for k in IS_KOLU_KATEGORILERI[is_kolu] if k in kategoriler]
    if not izinli:          # kisit is_kolu ile celisiyorsa yok say
        _KISIT_ONBELLEK[onbellek_anahtari] = None
        return None
    havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    if urun_deseni:
        r = re.compile(urun_deseni, re.IGNORECASE)
        for kat in izinli:
            alt = [u for u in ACIKLAMA_HAVUZU[kat] if r.search(u)]
            if len(alt) >= ASGARI_ALT_HAVUZ:
                havuzlar[kat] = alt
    sonuc = (izinli, havuzlar)
    _KISIT_ONBELLEK[onbellek_anahtari] = sonuc
    return sonuc


def firma_kisit_anahtari(is_kolu: IsKolu, satici_unvan: str) -> str | None:
    """Firma adi kisitinin KIMLIGI (eslesen desen). mutfak_anahtari ile ayni
    amac: fatura_no_tekrari cifti AYNI kisittan secilmeli, yoksa f2 devraldigi
    unvan ile kendi kalemleri celisir ('Kirtasiye' adi + ofis masasi kalemi).

    NEGATIF kisit da anahtarin PARCASI (2026-08-01): eskiden yalniz POZITIF
    kisita bakiyordu, oysa `ulasim_saglayici`da pozitif kisit hic yok -- cift
    serbestce eslesiyor ve f2 'Umut Seyahat' unvanini devralip kendi 'Akaryakit'
    kalemini koruyordu (120k'da 7 kayit). Iki kaynagi birlestirip donuyoruz."""
    ad = _ascii_kucuk(satici_unvan)
    pozitif = None
    for desen, _, _ in FIRMA_ADI_KISITLARI.get(is_kolu) or ():
        if re.search(desen, ad):
            pozitif = desen
            break
    negatif = None
    for desen, _ in FIRMA_ADI_YASAK_URUN.get(is_kolu) or ():
        if re.search(desen, ad):
            negatif = desen
            break
    if pozitif is None and negatif is None:
        return None
    return f"{pozitif}||{negatif}"


def mutfak_anahtari(satici_unvan: str) -> str | None:
    """Firma adinin isaret ettigi dar mutfagin KIMLIGI. Ayni anahtar = ayni menu
    kisiti. Gruplama icin kullanilir (bkz. anomaly_injector.fatura_no_tekrari_uygula:
    ayni fatura_no'yu paylasan cift ayni mutfaktan secilmeli, yoksa devralinan
    unvan ile kalemler celisir). Eslesme yoksa None."""
    ad = _ascii_kucuk(satici_unvan)
    for desen, _ in MUTFAK_KISITLARI:
        if re.search(desen, ad):
            return desen
    return None


def mutfak_havuzu_sec(satici_unvan: str) -> list[str] | None:
    """Firma adindan dar mutfak tespit eder ve o mutfaga uygun kalem havuzunu
    dondurur. Eslesme yoksa None -> kisit uygulanmaz (genis mutfaklar: kebap,
    doner, lokanta, ocakbasi, pide serbest kalir)."""
    if not YEMEK_MENU_BOLUMLERI:
        return None
    anahtar = mutfak_anahtari(satici_unvan)
    if anahtar is None:
        return None
    bolumler = next(b for d, b in MUTFAK_KISITLARI if d == anahtar)
    havuz = [u for b in bolumler for u in YEMEK_MENU_BOLUMLERI.get(b, [])]
    return havuz or None


def _sektor_kelime_sec(is_kolu: IsKolu, kalemler=None) -> str:
    """İş koluna uygun sektör kelimesini, fişteki kalemlerle TUTARLI olacak şekilde
    seçer. Hizmete özel kelimeler (Taksi vb.) yalnız ilgili kalem varsa aday olur;
    eşleşen özel kelime, jeneriklere göre daha yüksek olasılıkla seçilir."""
    adaylar = IS_KOLU_SEKTOR_KELIME[is_kolu]
    kalem_metni = _ascii_kucuk(" ".join(getattr(k, "aciklama", "") for k in (kalemler or [])))
    uygun: list[str] = []
    agirlik: list[int] = []
    for kelime in adaylar:
        kosul = SEKTOR_KELIME_KALEM_KOSULU.get(kelime)
        if kosul is None:
            uygun.append(kelime)       # jenerik -> her zaman uygun
            agirlik.append(1)
        elif kalem_metni and any(a in kalem_metni for a in kosul):
            uygun.append(kelime)       # özel ama fişte gerçekten var
            agirlik.append(3)          # tutarlı özel kelimeyi öne çıkar
    if not uygun:
        # Hiç uygun yok (ör. ulaşım firması ama fişte yalnız 'Uçak Bileti'):
        # koşullu olmayan (jenerik) kelimelere düş; o da yoksa ilk adaya.
        jenerikler = [k for k in adaylar if k not in SEKTOR_KELIME_KALEM_KOSULU]
        return random.choice(jenerikler) if jenerikler else adaylar[0]
    return random.choices(uygun, weights=agirlik, k=1)[0]

"""SUFFIX_HAVUZU = ["Ltd. Şti.", "A.Ş.", "Tic. Ltd. Şti."] """

IS_KOLU_SUFFIX = {
    IsKolu.RESTORAN: ["Gıda San. ve Tic. Ltd. Şti.", "Ltd. Şti."],
    IsKolu.MARKET: ["Gıda Paz. Tic. Ltd. Şti.", "Tic. A.Ş."],
    IsKolu.OTEL: ["Turizm A.Ş.", "Otelcilik Ltd. Şti."],
    IsKolu.TEKNOLOJI: ["A.Ş.", "Ltd. Şti."],
    IsKolu.DANISMANLIK_FIRMASI: ["Danışmanlık A.Ş.", "Ltd. Şti."],
    IsKolu.LOJISTIK_FIRMASI: ["A.Ş.", "Nak. Tic. Ltd. Şti."],
    IsKolu.ULASIM_SAGLAYICI: ["Ltd. Şti.", "Turizm Taş. Ltd. Şti."],
    IsKolu.OFIS_TEDARIK: ["Tic. Ltd. Şti.", "A.Ş."],
    # NOT: bu anahtar eskiden İKİ KEZ yazılmıştı (ikincisi birincisini sessizce
    # eziyordu); değerler aynı olduğu için etkisizdi, kopyala-yapıştır artığı silindi.
    IsKolu.ORGANIZASYON: ["Ltd. Şti.", "Prodüksiyon A.Ş."],
}

# Uzun unvan varyasyonlari için ek kelime havuzu
# "Diş" -> "Dış" (2026-07-30): anlam hatasiydi, 'İç ve Dış Ticaret' (domestic &
# foreign trade) kastediliyor; 'Diş' = tooth. Sinir->Sınır ile ayni sinif.
UZUN_UNVAN_EKLERI = ["Global", "İç ve Dış Ticaret", "Sanayi ve Ticaret"]

FIRMA_TURU_AGIRLIK = {
    FirmaTuru.KISA_UNVAN: 55,
    FirmaTuru.UZUN_UNVAN: 25,
    FirmaTuru.SAHIS_SIRKETI: 15,
    FirmaTuru.YABANCI_ORTAKLI: 5,
}


# 2.2 — Alan üretici fonksiyonlar

def soyisimleri_yukle() -> list[str]:
    """SQL dosyasindaki INSERT satirlarindan soyisimleri regex ile çeker."""
    with open(SOYISIM_SQL_DOSYASI, "r", encoding="utf-8") as f:
        icerik = f.read()

    # 'ABAT' gibi tek tirnak içindeki değerleri yakalar
    bulunanlar = re.findall(
    r"VALUES\s*\('([^']+)'\)",
    icerik,
    flags=re.IGNORECASE
)
    return bulunanlar


# modül yüklenince bir kere okunur; birleşen nokta yüklemede temizlenir
TUM_SOYISIMLER = [_birlesen_nokta_temizle(s) for s in soyisimleri_yukle()]


def rastgele_soyisim() -> str:
    return random.choice(TUM_SOYISIMLER)

def rastgele_vkn() -> str:
    """
    Gerçek Türkiye VKN algoritmasina uygun 10 haneli vergi kimlik no üretir.
    Algoritma: ilk 9 hane rastgele, 10. hane bir checksum formülüyle hesaplanir.
    """
    digits = [random.randint(0, 9) for _ in range(9)]

    total = 0
    for i in range(9):
        d = (digits[i] + (9 - i)) % 10
        if d != 0:
            d = (d * (2 ** (9 - i))) % 9
            if d == 0:
                d = 9
        total += d

    check_digit = (10 - (total % 10)) % 10
    digits.append(check_digit)

    return "".join(str(d) for d in digits)

def rastgele_tckn() -> str:
    """
    Gerçek TCKN algoritmasina uygun 11 haneli TC Kimlik No üretir.
    İlk hane 0 olamaz. Son iki hane checksum'dir.
    """
    ilk_dokuz = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(8)]

    tek_toplam = sum(ilk_dokuz[0:9:2])   # 1,3,5,7,9. haneler (index 0,2,4,6,8)
    cift_toplam = sum(ilk_dokuz[1:8:2])  # 2,4,6,8. haneler (index 1,3,5,7)

    onuncu_hane = ((tek_toplam * 7) - cift_toplam) % 10
    ilk_on = ilk_dokuz + [onuncu_hane]

    on_birinci_hane = sum(ilk_on) % 10

    return "".join(str(d) for d in ilk_on + [on_birinci_hane])

def rastgele_firma_turu() -> FirmaTuru:
    """Sadece firma türünü ağirlikli olarak seçer."""
    turler = list(FIRMA_TURU_AGIRLIK.keys())
    agirliklar = list(FIRMA_TURU_AGIRLIK.values())
    return random.choices(turler, weights=agirliklar, k=1)[0]


def rastgele_kimlik_no(firma_turu: FirmaTuru) -> str:
    if firma_turu == FirmaTuru.SAHIS_SIRKETI:
        return rastgele_tckn()
    return rastgele_vkn()

# faker Türkçe adlara akademik/mesleki unvan ekleyebiliyor ('Dr. İşcan Gülen',
# 'Doç. Zinnure Bilir Durdu', 'Arş. Gör. Nazi Hekime Zengin'). Bir MARKET'in
# unvanı "Doç. ..." olamaz. Çok parçalı unvanlar için TEKRARLI eşleşme (`+`).
# Ölçüldü (6000 ad): Öğr. Dr. Doç. Okt. Av. 'Arş. Gör.' 'Yrd. Doç.' Çev. Prof. Uz.
_UNVAN_ONEKLERI = re.compile(
    r"^(?:(?:Dr|Doç|Prof|Op|Av|Uz|Uzm|Yrd|Müh|Öğr|Gör|Arş|Okt|Çev)\.\s*"
    # Noktasiz hitap onekleri: Faker tr_TR "Bayan Feraye Sezer Alemdar" uretiyor.
    r"|(?:Bay|Bayan|Sayin|Sayın)\s+)+",
    re.IGNORECASE,
)

# Şahıs şirketinin TİCARİ UNVAN kelimesi. IS_KOLU_SEKTOR_KELIME'den AYRI tutulur
# çünkü oradaki kelimeler bir hukuki ekle birlikte kullanılmak üzere seçilmiş
# ('Lezzet' + 'Gida San. ve Tic. Ltd. Şti.'); tek başına bırakıldığında işletme
# adı gibi okunmuyor ('Oğuzman Çetin Lezzet'). Buradakiler TEK BAŞINA bir dükkân
# adı tamamlar ('Oğuzman Çetin Lokantası').
SAHIS_TICARI_UNVAN = {
    IsKolu.RESTORAN: ["Lokantası", "Restoran", "Kebap Salonu", "Kafe"],
    IsKolu.MARKET: ["Market", "Bakkaliyesi", "Gıda", "Şarküteri"],
    IsKolu.OTEL: ["Oteli", "Pansiyonu", "Konukevi"],
    IsKolu.OFIS_TEDARIK: ["Kırtasiye", "Büro Malzemeleri", "Ofis Market"],
    IsKolu.TEKNOLOJI: ["Bilişim", "Bilgisayar", "Teknoloji"],
    IsKolu.DANISMANLIK_FIRMASI: ["Danışmanlık", "Müşavirlik", "Denetim"],
    IsKolu.LOJISTIK_FIRMASI: ["Nakliyat", "Kargo", "Lojistik"],
    IsKolu.ULASIM_SAGLAYICI: ["Turizm", "Taşımacılık", "Oto Kiralama"],
    IsKolu.ORGANIZASYON: ["Organizasyon", "Etkinlik", "Davet Evi"],
}


def _ayni_kelimeyi_tasiyor(a: str, b: str) -> bool:
    """İki unvan parçası ORTAK bir anlamlı kelime taşıyor mu (diakritik/büyük-küçük
    duyarsız). Hukuki kısaltmalar ('A.Ş.', 'Tic.', 've') ortak sayılmaz -- onlar
    her suffix'te bulunur, sayılsa hiçbir aday kalmazdı."""
    _yoksay = {"a.ş.", "as", "ltd.", "şti.", "sti", "tic.", "tic", "san.", "san",
               "ve", "paz.", "paz", "nak.", "nak", "taş.", "tas"}
    def _kelimeler(m: str) -> set[str]:
        return {_ascii_kucuk(k) for k in m.split()
                if _ascii_kucuk(k) not in _yoksay and len(k) > 2}
    return bool(_kelimeler(a) & _kelimeler(b))


# Faker'ın tr_TR ad havuzunda BOZUK KARAKTERLİ girdiler var: 'N˜zamett˜n'
# (Nizamettin) -- '˜' Latin-1 aralığında olduğu için `latin_disi_mi` süzgeci
# yakalamıyor. Ölçüldü (registry 28.882): 7 firma, hepsi kaynak=sahis. Fişte
# satıcı adı olarak göründüğünde metne de sızıyordu ("N˜zamett˜n Bilgin Oteli
# ile yapılan konaklama"). ONARMAK yerine REDDEDİLİR: '˜' -> 'i' eşlemesi
# tahminden ibaret ve Faker havuzu sınırsız, yeni ad çekmek bedava.
_BOZUK_AD_KARAKTERI = re.compile(r"[˜¸ˇ˘´¨-]")


def rastgele_sahis_kisi_adi() -> str:
    """Şahıs şirketinin SAHİBİNİN adı soyadı. İşletme adından AYRIDIR: gerçek
    fişte iki ayrı satır basılır ('SAYLA MANTI' / 'FEYYAZ ESEN')."""
    # Bozuk karakterli ad çıkarsa yenisini çek (bkz. _BOZUK_AD_KARAKTERI).
    # Sınırlı deneme: havuz tükenmez ama sonsuz döngü de olmasın.
    for _ in range(10):
        kisi = _UNVAN_ONEKLERI.sub("", fake.name()).strip()
        if not _BOZUK_AD_KARAKTERI.search(kisi):
            break
    # Faker çok parçalı adlarda aynı soyadı iki kez üretebiliyor ('Duran Duran',
    # 13.200 adda 21 kez); fişte gülünç duruyor, ardışık tekrarı düşür.
    parcalar: list[str] = []
    for p in kisi.split():
        if not parcalar or _ascii_kucuk(p) != _ascii_kucuk(parcalar[-1]):
            parcalar.append(p)
    # Gerçek fişte AD + SOYAD basılır ('FEYYAZ ESEN', 'OSMAN TETİK'). Faker tr_TR
    # ise üç-dört parçalı ad üretebiliyor ('Feryas Nihan Öcalan Bilge'); ilk ad
    # ile son soyadı tutulur.
    if len(parcalar) > 2:
        parcalar = [parcalar[0], parcalar[-1]]
    return " ".join(parcalar)


def rastgele_firma_adi(is_kolu: IsKolu, firma_turu: FirmaTuru, kalemler=None) -> str:
    if firma_turu == FirmaTuru.SAHIS_SIRKETI:
        # ŞAHIS ŞİRKETİNDE İŞLETME ADI, SAHİBİNİN ADINDAN BAĞIMSIZDIR (2026-08-14).
        # Gerçek fiş kanıtı: "SAYLA MANTI / FEYYAZ ESEN", "FIORE ART PIZZA /
        # OSMAN TETIK" -- iki AYRI satır. Eskiden ikisini birleştirip tek satır
        # üretiyorduk ("İşcan Gülen Market"), o yüzden sahibinin adı hiç
        # basılmıyor, işletme adı da kişi adından türetilmiş oluyordu.
        # Sahibinin adı ayrı üretilir (rastgele_sahis_kisi_adi) ve registry'de
        # firmayla birlikte DONAR. Hukuki ek (A.Ş./Ltd. Şti.) EKLENMEZ -- o
        # tüzel kişiliğe aittir.
        on_ek = random.choice([rastgele_soyisim(), random.choice(NITELIK_KELIME_HAVUZU)])
        unvan = random.choice(SAHIS_TICARI_UNVAN[is_kolu])
        return f"{on_ek} {unvan}"

    # Sektör kelimesi fişteki kalemlerle tutarlı seçilir (ör. 'Taksi' yalnız
    # faturada taksi kalemi varsa). kalemler verilmezse jeneriklere düşülür.
    sektor_kelime = _sektor_kelime_sec(is_kolu, kalemler)
    # SEKTÖR KELİMESİ ile SUFFIX AYNI KELİMEYİ TAŞIYABİLİR -- şablon ikisini yan
    # yana koyduğu için unvanda birebir tekrar doğuyordu (2026-07-30'da ölçüldü):
    #   'Manav' + 'Danışmanlık' + 'Danışmanlık A.Ş.'  -> 'Manav Danışmanlık Danışmanlık A.Ş.'
    # Aynı çakışma ULASIM ('Turizm' + 'Turizm Taş.'), GIYIM ('Tekstil' + 'Tekstil
    # Tic.'), KISISEL_BAKIM ('Kozmetik' + 'Kozmetik Tic.') ve ORGANIZASYON
    # ('Prodüksiyon' + 'Prodüksiyon A.Ş.') için de vardı; tek tek düzeltmek yerine
    # seçim aşamasında engellenir. Tüm adaylar çakışıyorsa kısıt uygulanmaz
    # (hukuki ek unvanın zorunlu parçası, atlanamaz).
    # Çakışma denetimi İKİ AŞAMALI (ikincisi 2026-07-30'da eklendi):
    #  (1) AYNI kelime      : 'Danışmanlık' + 'Danışmanlık A.Ş.'
    #  (2) FARKLI ama AYNI EKSENDEN iki sektör kelimesi: 'Consulting' +
    #      'Danışmanlık A.Ş.' (aynı şeyin iki dildeki karşılığı), 'Organizasyon'
    #      + 'Prodüksiyon A.Ş.'. Ölçüldü: sentetik adların bir kısmı böyle
    #      çıkıyordu. Ölçüt suffix'in bu iş kolunun sektör kelimelerinden
    #      HERHANGİ birini taşıyıp taşımadığı -- ad zaten bir sektör kelimesi
    #      aldıysa suffix'in ikincisini getirmesine gerek yok.
    _sektor_kelimeleri = IS_KOLU_SEKTOR_KELIME[is_kolu]
    adaylar = [
        s for s in IS_KOLU_SUFFIX[is_kolu]
        if not _ayni_kelimeyi_tasiyor(sektor_kelime, s)
        and not any(_ayni_kelimeyi_tasiyor(sk, s) for sk in _sektor_kelimeleri)
    ]
    suffix = random.choice(adaylar or IS_KOLU_SUFFIX[is_kolu])
    ozel_isim = rastgele_soyisim()

    sablon = random.choices(
        [
            "isim_once",       # Yilmaz Gida A.Ş.
            "isim_yok",        # Anadolu Gida A.Ş. (nitelik kelimesi sektörün yerini dolduruyor)
            "isim_nitelikli",  # Yilmaz Anadolu Gida A.Ş.
        ],
        weights=[40, 20, 40],
        k=1,
    )[0]

    if sablon == "isim_yok":
        nitelik = random.choice(NITELIK_KELIME_HAVUZU)
        return f"{nitelik} {sektor_kelime} {suffix}"

    if sablon == "isim_nitelikli":
        nitelik = random.choice(NITELIK_KELIME_HAVUZU)
        return f"{ozel_isim} {nitelik} {sektor_kelime} {suffix}"

    return f"{ozel_isim} {sektor_kelime} {suffix}"   # isim_once (varsayilan)




""" fatura kategorisi tutarlılık için random.choices(izinli_kategoriler) ile oluşturuluyor bu kod bloğu şuanlık ölü.
def rastgele_kategori() -> HarcamaKategorisi:
    kategoriler = list(HarcamaKategorisi)
    # Sira: YEMEK_HIZMETI, TEMEL_GIDA, ULASIM_HIZMETI, ULASIM_BIREYSEL, KONAKLAMA,
    #       OFIS_SARF_MALZEME, OFIS_DEMIRBAS, YAZILIM_LISANS, DANISMANLIK, ALKOL, EGLENCE, DIGER
    agirliklar = [15, 15, 7, 8, 8, 9, 3, 10, 8, 5, 5, 7, 1 ,1] #sondaki 1, 1 sonradan eklelenen tütün ürünleri ve kumar için
    # iskoluna dahil edilip normal fatura üretiminde hiç kullanilmayacaklar 
    # yalnizca anomali kisminda dahil olacak ama liste uzunluğu enumerator ile eşleşmeli bundan dolayi eklendi
    return random.choices(kategoriler, weights=agirliklar, k=1)[0]
"""

def rastgele_aciklama(kategori: HarcamaKategorisi) -> str:
    return random.choice(ACIKLAMA_HAVUZU[kategori])


def rastgele_birim(kategori: HarcamaKategorisi) -> str:
    return random.choice(BIRIM_HAVUZU[kategori])


# --- ÜRÜN -> BİRİM ÇIKARIMI (2026-08-01) ------------------------------------
#
# SORUN (ölçüldü): `rastgele_birim` kategori havuzundan KÖRLEMESİNE seçiyor,
# ürünün ne olduğuna hiç bakmiyordu. Sonuç: 'Litre' birimli 19.667 kalemin
# %89,3'ü katiydi -- '0,58 Litre Toz Şeker', 'Litre Çiğ Köfte 200Gr',
# '9,41 Litre Şenpiliç Ciğer'. Ondalik miktar kusurundan (TAM_SAYI_BIRIMLERI)
# AYRI bir eksen: orada miktarin biçimi, burada birimin KENDİSİ yanliş.
#
# Kalip `FIYAT_ARALIGI_URUN_TIPI` ile AYNI: kategori başina (desen, birim)
# listesi, İLK EŞLEŞEN kazanir. Hiçbiri tutmazsa eski rastgele davranişa düşer
# -- bu KASITLI: belirsizliğin GERÇEK olduğu yerde çeşitlilik korunmali
# (danişmanlik gerçekten hem saatlik hem aylik faturalanir).
#
# Desenler `_ascii_kucuk` çiktisi üzerinde çalişir; tabloya ASCII yaz. Veri hem
# 'Süt' hem 'Sut', hem 'Kağidi' hem 'Kagidi' içeriyor (CLAUDE.md §7 ek/yumuşama
# tuzaği). Ek biçimleri için sondaki `\b`'den KAÇIN: 'kagit' -> 'kağidi',
# 'bilet' -> 'bileti', 'kiralama' -> 'kiralamasi'.

# Paket/ambalaj boyutu iŞARETİ: '250Gr', '1Lt', '1,5 Litre', '0,5 L', '370 Ml',
# "30 Lu", "4'Lu", '10 Adet'. Adi boyut taşiyan ürün AMBALAJLIDIR -> market
# fişinde ADETLE satilir (1 kg'lik bulgur paketi 'Adet'tir, '0,58 Kg' değil).
#
# YAZIYLA yazilan biçim de tutmali: ilk sürüm yalniz 'lt' arayinca
# 'Saka Su 1,5 Litre' ambalaj sayilmadi ve '0,96 Litre Saka Su 1,5 Litre'
# gibi KENDİ İÇİNDE ÇELİŞEN satir üretti. Kesirli boyut ('1,5') ve kesme
# işareti ("4'Lu") de hesaba katilir. Alternatifler UZUNDAN KISAYA siralanir
# ('litre' > 'lt' > 'l'), yoksa kisa olan uzunu maskeler.
_AMBALAJ_BOYUTU = re.compile(
    r"\d+(?:[.,]\d+)?\s*['’]?\s*(gram|gr|kg|litre|lt|ml|cl|adet|lu|li|l)\b")

BIRIM_DESENLERI: dict[HarcamaKategorisi, list[tuple[re.Pattern, str]]] = {
    HarcamaKategorisi.TEMEL_GIDA: [
        (_AMBALAJ_BOYUTU, "Adet"),
        # Ambalaj işareti YOKSA açik/dökme satilir. Önce sıvı, sonra tartılı.
        # ÜNSÜZ YUMUŞAMASI (CLAUDE.md §7): sondaki \b kullanmiyoruz ki ekli
        # biçimler de tutsun ('ciger'->'cigeri'), ama k/t/p SESSİZİ YUMUŞAYAN
        # kökler ayrica yazilmali: 'fistik'->'fistigi', 'cekirdek'->'cekirdegi',
        # 'kanat'->'kanadi'. Düz 'fistik' deseni 'Yer Fistigi'ni KAÇIRIYORDU.
        (re.compile(r"\bsu\b|\bsut\b|ayran|meyve suyu|zeytinyag|sivi yag|sirke"), "Litre"),
        (re.compile(
            r"domates|salatalik|patlican|biber|sogan|patates|havuc|kabak|marul"
            r"|ispanak|lahana|brokoli|pirasa|kereviz|turp|roka|maydanoz|semizotu"
            r"|dere ?otu|nane|tere\b|kivircik|pazi|bakla"
            r"|elma|armut|muz|portakal|mandalina|limon|uzum|karpuz|kavun|seftali"
            r"|kayisi|erik|cilek|kiraz|incir|nar\b|ayva|avokado|ananas|kivi"
            r"|greyfurt|nektarin|visne|dut\b|hurma|kestane"
            r"|\bet\b|kiyma|kusbasi|kuzu|dana|tavuk|pilic|ciger|kana[td]|balik"
            r"|hamsi|somon|levrek|cupra|karides|midye|kalkan"
            r"|peynir|ezine|kasar|lor\b|coke?lek|zeytin"
            r"|leblebi|fisti[kg]|findik|ceviz|badem|kuruyemis|cekirde[kg]"
            r"|seker|bulgur|pirinc|mercimek|nohut|fasulye|bugday|\bun\b|irmik"
            r"|kurabiye|helva|pastirma|sucuk|salam|jambon"
        ), "Kg"),
        # KALAN HER ŞEY AMBALAJLI MAMUL -> Adet. Bu terminal kural KASITLI:
        # eskiden buradan `rastgele_birim`e düşülüyordu ve 41.456 ürünün 1/3'ü
        # 'Litre' aliyordu ('Litre Ulker Halley', 'Litre Toz Şeker'). Artik
        # Litre/Kg YALNIZCA olumlu teşhisle atanir; belirsizlik ADET'e gider.
        (re.compile(r".", re.S), "Adet"),
    ],
    # ULASIM_HIZMETI / ULASIM_BIREYSEL / KONAKLAMA / DANISMANLIK BURADA YOK --
    # KASITLI. Bu dört kategorinin havuzu el yazimi kucuk CSV'lerden gelir ve
    # urun basina `birim` kolonu TASIR (olculdu 2026-08-01: 21/21, 23/23, 24/24,
    # 23/23 -> %100). `birim_sec`'te CSV her zaman kazandigi icin buraya desen
    # yazmak OLU KOD olurdu; ileride biri deseni duzeltip etkisini goremezdi.
    # Bu kategorilerde birim sorunu varsa CSV'DE duzeltilir
    # (data/hizmet_verileri/*.csv), burada degil.
    HarcamaKategorisi.YEMEK_HIZMETI: [
        (re.compile(r"menu|acik bufe|bufe|kisi basi|fix|set\b|ziyafet"), "Kişi"),
        # Tek tabak/içecek KİŞİYLE satilmaz. Terminal kural olmadan havuzun
        # %99,4'u rastgeleye düşüyordu -> '3 Kişi Cappuccino'.
        (re.compile(r".", re.S), "Adet"),
    ],
    HarcamaKategorisi.EGLENCE: [
        (re.compile(r"bilet|jeton"), "Adet"),
        (re.compile(r"giris|katilim|cover charge"), "Kişi"),
        (re.compile(r".", re.S), "Adet"),
    ],
    HarcamaKategorisi.TUTUN_URUNLERI: [
        # Havuzun tamami sigara markasi ('Winston Kisa Box', 'Tekel 2001 Soft').
        # Sigara PAKETLE satilir; 'Adet' tekli sigara demek olurdu.
        (re.compile(r".", re.S), "Paket"),
    ],
    HarcamaKategorisi.ALKOL: [
        # '\bcl\b' YAZMA: '(70cl)' ascii'de 'raki (70cl)' olur ve 0 ile c
        # arasinda kelime siniri YOKTUR -> desen kacirirdi. Sayiyla birlikte yaz.
        (re.compile(r"sise|fici|\d+\s*(cl|ml)"), "Şişe"),
        # Duble/tek/kadeh/shot/kokteyl = servis porsiyonu; kalani da öyle
        # (Şampanya/Likör/Konyak bardakla servis edilir).
        (re.compile(r".", re.S), "Adet"),
    ],
    HarcamaKategorisi.OFIS_SARF_MALZEME: [
        # Adi '(500'lü)', '(10'lu)', 'Kutusu', 'Seti' diyen ürün KUTUDUR.
        (re.compile(r"kutu|\bseti\b|\d+\s*'?\s*(lu|li)\b|koli|zimba teli|atas"
                    r"|klips|zarf"), "Kutu"),
        (re.compile(r".", re.S), "Adet"),
    ],
    HarcamaKategorisi.YAZILIM_LISANS: [
        (re.compile(r"kullanici|\bkull\b|cihaz|\bpc\b|server|sunucu|mobil"), "Kullanici"),
        (re.compile(r"abonelik|saas|bulut|aylik|hosting"), "Ay"),
        (re.compile(r".", re.S), "Adet"),
    ],
}


def birim_sec(kategori: HarcamaKategorisi, aciklama: str) -> str:
    """Ürün adindan gerçekçi birim çikarir; desen tutmazsa rastgeleye düşer.

    Öncelik: CSV'deki açik eşleme (URUN_BIRIM_ESLEME) > desen > rastgele.
    """
    csv_birim = URUN_BIRIM_ESLEME.get(aciklama)
    if csv_birim:
        return csv_birim
    ad = _ascii_kucuk(aciklama)
    for desen, birim in BIRIM_DESENLERI.get(kategori, ()):
        if desen.search(ad):
            return birim
    return rastgele_birim(kategori)


def _birim_desenlerini_dogrula() -> None:
    """Import aninda denetler: üretilebilen her (kategori,birim) çifti hem
    BIRIM_HAVUZU'nda hem de bir fiyat katmaninda tanimli mi?

    İKİ BİRİM KAYNAĞI VAR, ikisi de denetlenmeli (2026-08-01):
      1. Küçük EL YAZIMI CSV'ler (danişmanlik/konaklama/ulaşim) ürün başina
         `birim` kolonu taşir -> URUN_BIRIM_ESLEME. Otoriterdir.
      2. Büyük CSV'lerde birim kolonu YOK -> desen, o da tutmazsa kategori
         bazli rastgele (BIRIM_HAVUZU).
    Birinci kaynak havuzun DIŞINA çikabiliyordu: ulasim_hizmeti ürünleri
    CSV'den 'Ay'/'Saat' aliyordu ama (kategori,birim) fiyat katmani yoktu ve
    üretim sessizce FIYAT_ARALIGI_GENEL'e düşüyordu -- CSV yolu hatasiyla ayni
    sinif sessiz bozulma (CLAUDE.md §7)."""
    # YALNIZ "havuzda yok" denetlenir. "Fiyat katmani yok" KASITLI OLARAK
    # denetlenmez: FIYAT_ARALIGI_GENEL'e dusmek fallback'in ta kendisidir
    # (ulasim_bireysel/'Adet' boyle calisir ve dogrudur). Onu da uyari yapmak
    # 20+ satir gurultu uretip uyarilari okunmaz kilardi.
    def _bildir(kaynak, kategori, birim, ek=""):
        havuz = set(BIRIM_HAVUZU.get(kategori, []))
        if birim not in havuz:
            print(f"  UYARI birim[{kaynak}]: {kategori.value} -> '{birim}' "
                  f"BIRIM_HAVUZU'nda yok (havuz: {sorted(havuz)}){ek}")

    for kategori, kayitlar in BIRIM_DESENLERI.items():
        for _, birim in kayitlar:
            _bildir("desen", kategori, birim)

    for kategori, havuz in ACIKLAMA_HAVUZU.items():
        for ad in havuz:
            birim = URUN_BIRIM_ESLEME.get(ad)
            if birim:
                _bildir("csv", kategori, birim, f"  <- '{ad[:40]}'")


_birim_desenlerini_dogrula()


# ÜRÜN TİPİ fiyat araliği -- ÜÇÜNCÜ ve EN ÖNCELIKLI katman (2026-07-30).
#
# Mevcut iki katman (kategori, birim) ve (kategori) bazliydi; kategori icindeki
# fiyat DAGILIMINI ayirt edemiyor. Havuza ucuz sarf malzemesi girince
# (havlu kagidi, cop poseti, pecete) bunlar TEMIZLIK/OFIS_SARF'in genis
# araligindan fiyat aldi ve absurt satirlar dogdu -- OLCULDU (25k batch):
#   6 x 32.574 TL = 195.444 TL  'F Saff Havlu Kagidi 12 Li'
#   1 x 16.065 TL =  16.065 TL  'Selin Kolonya 400Ml'
# Gunluk sarf kategorilerinde 15.000 TL ustu 284 satir (%0,43).
# Model 195 bin TL'lik pecete icin kurumsal gerekce yazamaz (§17: aciklama
# kalitesi FISE baglidir).
#
# SIRA ONEMLI, ilk eslesen kazanir. Desenler ADA bakar, kategoriye degil --
# ayni urun tipi birden fazla kategoride gecebiliyor.
# ---------------------------------------------------------------------------
# FIYAT KATMANLARI -- daralan hiyerarsi (2026-07-30'da ucuncu basamak eklendi):
#     kategori  ->  (kategori, birim)  ->  (kategori, urun tipi)
#
# NEDEN URUN TIPI KATEGORININ ALTINDA: ilk surumde urun tipi KATEGORIDEN BAGIMSIZ
# duz bir listeydi ve dogrudan bug uretti -- 'batarya' deseni hem OFIS_SARF
# (esik 5) hem TEKNOLOJI_EKIPMAN (esik 300) kalemlerine ayni araligi veriyordu,
# 40-300 TL arasi her telefon bataryasi TEMIZ faturayi sahte `dusuk_ondalik_
# kaymasi` etiketine dusuruyordu. Olculdu: dusuk_ondalik_kaymasi 2.627 -> 5.499,
# anomali orani 0,2586 -> 0,2764.
#
# SOZLESME KATEGORIYLEDIR: validators kalem duzeyinde YALNIZ harcama_kategorisi'ne
# bakar (is_kolu'yu hic gormez). O yuzden guvenlik cipasi kategori olmali;
# is_kolu ustte IS_KOLU_KATEGORILERI ile zaten baglanmis durumda.
#
# ARALIK YAZARKEN: asagidaki _fiyat_araliklarini_dogrula import aninda kontrol
# eder ve bandin disina cikan araligi GURULTULU sekilde reddeder. Sessiz kirpma
# YOK -- yanlis bir deger yazarsan ekranda gorursun.
FIYAT_ARALIGI_URUN_TIPI: dict[HarcamaKategorisi, list[tuple[re.Pattern, tuple[int, int]]]] = {
    # Akaryakit litre fiyatlari birbirinden ayrisir (otogaz benzinin yarisi).
    # Gercek fis dogrulamasi: 'KURSUNSUZ BNZ95' 51,98 TL/lt (11.10.2025).
    HarcamaKategorisi.ULASIM_BIREYSEL: [
        (re.compile(r"otogaz|lpg", re.I), (25, 33)),
        (re.compile(r"adblue", re.I), (28, 45)),
        (re.compile(r"benzin|kur[sş]unsuz", re.I), (50, 66)),
        (re.compile(r"motorin|diesel|dizel", re.I), (48, 64)),
    ],
    HarcamaKategorisi.TEMIZLIK: [
        (re.compile(r"havlu ?ka[g\u011f]|pe[c\u00e7]ete|tuvalet ka[g\u011f]|[\u0131i]slak mendil|mendil", re.I), (30, 220)),
        (re.compile(r"[c\u00e7][o\u00f6]p (torba|po[s\u015f]et)|buzdolab[\u0131i] po[s\u015f]|po[s\u015f]et", re.I), (30, 180)),
        (re.compile(r"eldiven|s[u\u00fc]nger|bula[s\u015f][\u0131i]k teli|ovma", re.I), (30, 160)),
        (re.compile(r"stre[c\u00e7] film|al[u\u00fc]minyum folyo", re.I), (30, 200)),
        (re.compile(r"kolonya|sabun|deterjan|[c\u00e7]ama[s\u015f][\u0131i]r suyu|yumu[s\u015f]at", re.I), (30, 450)),
    ],
    HarcamaKategorisi.OFIS_SARF_MALZEME: [
        (re.compile(r"kartvizit", re.I), (150, 900)),
        (re.compile(r"toner|kartu[s\u015f]|dolum", re.I), (700, 6000)),
        (re.compile(r"\ba4\b|\ba3\b|fotokopi ka[g\u011f][\u0131i]d|yaz[\u0131i]c[\u0131i] ka[g\u011f]", re.I), (150, 900)),
        (re.compile(r"ban[td]\b|band[\u0131i]|yap[\u0131i][s\u015f]t[\u0131i]r|z[\u0131i]mba|ata[s\u015f]", re.I), (50, 250)),
        (re.compile(r"kalem|silgi|kalemtira[s\u015f]|cetvel|defter|bloknot|dosya|klas[o\u00f6]r"
                    r"|zarf|delge[c\u00e7]|klips|ar[s\u015f]iv kutu|not ka[g\u011f][\u0131i]d", re.I), (50, 320)),
    ],
    HarcamaKategorisi.KISISEL_BAKIM: [
        (re.compile(r"a[g\u011f]da|epilasyon", re.I), (55, 450)),
        (re.compile(r"kolonya|sabun|[s\u015f]ampuan|di[s\u015f] f[\u0131i]r[c\u00e7]a|di[s\u015f] macun", re.I), (55, 450)),
    ],
}


# Asagi/yukari fat-finger bandinin carpanlari. BURADA tanimli cunku validators.py
# zaten bu modulden import ediyor (ters yon dairesel olurdu) ve iki tarafin AYNI
# sayiyi kullanmasi sart: uretim bu bandin disina ciktigi anda TEMIZ fatura
# kendiliginden ondalik-kaymasi etiketi alir.
ONDALIK_KAYMASI_ALT_BANT_CARPANI = Decimal("10")
ONDALIK_KAYMASI_UST_BANT_CARPANI = Decimal("5")

# rastgele_birim_fiyat'in %10'luk "gurultu" dalinin aralik disina tasma orani.
# Dogrulama bu tasmayi HESABA KATAR, aksi halde aralik guvenli gorunup uretim
# yine bandin disina cikardi.
FIYAT_TASMA_ORANI = 0.3


def _guvenli_bant(kategori: HarcamaKategorisi) -> tuple[float, float]:
    """Kategorinin ondalik-kaymasi etiketi ALMAYAN fiyat araligi."""
    lo, hi = FIYAT_ARALIGI_GENEL[kategori]
    return (lo / float(ONDALIK_KAYMASI_ALT_BANT_CARPANI),
            hi * float(ONDALIK_KAYMASI_UST_BANT_CARPANI))


def _fiyat_araliklarini_dogrula() -> list[str]:
    """Her (kategori, urun tipi) araligi -- TASMA DAHIL -- kategorinin guvenli
    bandinin icinde mi? Import aninda calisir; ihlali sessizce duzeltmez,
    listeler. Ayni denetim (kategori, birim) katmanina da uygulanir."""
    hatalar: list[str] = []

    def _kontrol(kategori, etiket, low, high):
        # DENETIM, URETIMIN GERCEKTE YAPTIGIYLA AYNI OLMALI:
        #  - %90'lik triangular dal asla `low`un ALTINA inmez -> alt kontrol `low`
        #  - %10'luk tasma dali zaten `max(bant_alt, low - tasma)`ya kirpiliyor,
        #    yani alt taraf yapisal olarak guvenli; onu ayrica denetlemek her
        #    araligi uyari olarak isaretler ve uyarilari anlamsizlastirirdi.
        #  - UST tarafta kirpma YOK, o yuzden tasma HESABA KATILIR.
        bant_alt, bant_ust = _guvenli_bant(kategori)
        if low < bant_alt:
            hatalar.append(
                f"{kategori.value}/{etiket}: alt sinir {low:.0f} < guvenli {bant_alt:.0f}"
                f" -> sahte dusuk_ondalik_kaymasi")
        efektif_ust = high + (high - low) * FIYAT_TASMA_ORANI
        if efektif_ust > bant_ust:
            hatalar.append(
                f"{kategori.value}/{etiket}: ust {efektif_ust:.0f} > guvenli {bant_ust:.0f}"
                f" -> sahte ondalik_kaymasi")
    for kategori, kayitlar in FIYAT_ARALIGI_URUN_TIPI.items():
        for desen, (low, high) in kayitlar:
            _kontrol(kategori, desen.pattern[:24], low, high)
    for (kategori, birim), (low, high) in FIYAT_ARALIGI_DETAYLI.items():
        _kontrol(kategori, birim, low, high)
    return hatalar


_FIYAT_ARALIK_HATALARI = _fiyat_araliklarini_dogrula()
if _FIYAT_ARALIK_HATALARI:
    print("[!] FIYAT ARALIGI UYARISI -- bu araliklar TEMIZ faturada sahte anomali uretir:")
    for _h in _FIYAT_ARALIK_HATALARI:
        print(f"      {_h}")


def _politika_limitlerini_dogrula() -> list[str]:
    """Her (kategori, birim) icin politika limiti, uretimin cikardigi en yuksek
    fiyatin (tasma dahil) ustunde mi? Altindaysa TEMIZ fatura da limit_asimi alir.
    Fiyat tablolari burada oldugu icin denetim politika.py'de degil burada."""
    hatalar: list[str] = []
    for kategori, birimler in BIRIM_HAVUZU.items():
        # Urun tipi katmani birimden bagimsiz eslesir -> her birim icin gecerli.
        urun_tipi_tavan = max(
            (high + (high - low) * FIYAT_TASMA_ORANI
             for _, (low, high) in FIYAT_ARALIGI_URUN_TIPI.get(kategori, ())),
            default=0.0,
        )
        for birim in birimler:
            if not isinstance(birim, str):
                continue
            limit = kalem_limiti(kategori, birim)
            if limit is None:
                continue
            low, high = FIYAT_ARALIGI_DETAYLI.get(
                (kategori, birim), FIYAT_ARALIGI_GENEL[kategori]
            )
            tavan = max(high + (high - low) * FIYAT_TASMA_ORANI, urun_tipi_tavan)
            if limit < tavan:
                hatalar.append(
                    f"{kategori.value}/{birim}: limit {limit:.0f} < uretim tavani {tavan:.0f}"
                    f" -> sahte limit_asimi")
    return hatalar


_POLITIKA_LIMIT_HATALARI = _politika_limitlerini_dogrula()
if _POLITIKA_LIMIT_HATALARI:
    print("[!] POLITIKA LIMIT UYARISI -- data/politika_limitleri.json duzeltilmeli:")
    for _h in _POLITIKA_LIMIT_HATALARI:
        print(f"      {_h}")


def _urun_tipi_araligi(kategori: HarcamaKategorisi, aciklama: str) -> tuple[int, int] | None:
    """Kategorinin urun tipi araliklari icinde ilk eslesen -- SIRA ONEMLI."""
    for desen, aralik in FIYAT_ARALIGI_URUN_TIPI.get(kategori, ()):
        if desen.search(aciklama):
            return aralik
    return None


def rastgele_birim_fiyat(kategori: HarcamaKategorisi, birim: str,
                         aciklama: str = "") -> Decimal:
    # Katman sirasi (daralandan genele): (kategori,urun tipi) -> (kategori,birim) -> kategori
    aralik = _urun_tipi_araligi(kategori, aciklama) if aciklama else None
    if aralik is None:
        aralik = FIYAT_ARALIGI_DETAYLI.get(
            (kategori, birim),
            FIYAT_ARALIGI_GENEL[kategori]
        )
    low, high = aralik

    # %90 ihtimalle normal aralikta, %10 ihtimalle aralik dişina taşan
    # "gürültülü" bir değer üret (gerçek hayatin kusurlu dağilimini simüle eder)
    if random.random() < 0.90:
        fiyat = random.triangular(low, high, low + (high - low) * 0.2)
    else:
        # Aralığın biraz dışına taşan aykırı (ama fahiş olmayan) değer.
        # ALT SINIR `max(0, ...)` DEGIL guvenli bandin tabani (2026-07-30):
        # tabani yuksek kategorilerde (OFIS_MOBILYA 2000, TEKNOLOJI 3000)
        # low - %30 tasma 0'a iniyordu ve 'Metal Kitaplik 93 TL' gibi fiyatlar
        # TEMIZ faturayi sahte `dusuk_ondalik_kaymasi`ya dusuruyordu. Bu, urun
        # tipi katmanindan ONCE de vardi.
        tasma = (high - low) * FIYAT_TASMA_ORANI
        bant_alt, _ = _guvenli_bant(kategori)
        fiyat = random.uniform(max(bant_alt, low - tasma), high + tasma)

    return Decimal(str(round(fiyat, 2)))


def kdv_orani_belirle(kategori: HarcamaKategorisi) -> float:
    return KDV_ORANI_MAP[kategori]

# BOLUNEMEZ birimler: gercek faturada kesirli yazilmasi anlamsizdir.
# 'Ay' ve 'Paket' 2026-08-01'de EKLENDI. Eksiklikleri fise dogrudan sizmisti:
# '2,72 Ay Siber Guvenlik Danismanligi', '1,96 Paket Muratti' (olculdu: Ay
# kalemlerinin %98,9'u, Paket'in %99,7'si ondalikti -> 7.427 fatura, %6,19).
# Yeni birim eklerken SOR: bu seyin yarisi satin alinabilir mi? Alinamiyorsa
# birim BIRIM_HAVUZU'na girerken ayni anda buraya da girmeli.
TAM_SAYI_BIRIMLERI = {"Adet", "Kutu", "Kişi", "Gece", "Gün", "Lisans", "Şişe",
                      "Kullanici", "Ay", "Paket"}

# YARIM ADIMLI birimler: bolunebilir ama gercek fatura ceyrek/yarim katlarinda
# kesilir. '2,68 Saat danismanlik' teknik olarak mumkun, ticari olarak degil.
YARIM_ADIM_BIRIMLERI = {"Saat"}

# Serbest ondalik kalanlar (Kg, Litre, Km, Ton) KASITLI olarak dokunulmadi --
# 0,58 Kg havuc / 3,34 Ton nakliye gercekci.

# Urun tipine gore MIKTAR araligi. Birim tek basina yetmez: ayni 'Litre'
# temel_gidada 0,5-10 (sut, yag), akaryakitta 15-60'tir (depo dolumu).
# Eslesme yoksa birim bazli varsayilana dusulur.
MIKTAR_ARALIGI_URUN_TIPI: dict[HarcamaKategorisi, list[tuple[re.Pattern, tuple[float, float]]]] = {
    HarcamaKategorisi.ULASIM_BIREYSEL: [
        (re.compile(r"adblue", re.I), (5.0, 20.0)),
        (re.compile(r"benzin|kur[sş]unsuz|motorin|diesel|dizel|otogaz|lpg", re.I), (12.0, 60.0)),
    ],
}


def rastgele_miktar(birim: str, kategori: HarcamaKategorisi | None = None,
                    aciklama: str = "") -> float:
    if kategori is not None and aciklama:
        for desen, (alt, ust) in MIKTAR_ARALIGI_URUN_TIPI.get(kategori, ()):
            if desen.search(aciklama):
                return round(random.uniform(alt, ust), 2)
    if birim in TAM_SAYI_BIRIMLERI:
        return float(random.randint(1, 10))
    if birim in YARIM_ADIM_BIRIMLERI:
        return round(random.uniform(0.5, 10) * 2) / 2   # [0,5 .. 10,0], 0,5 adim
    return round(random.uniform(0.5, 10), 2)

def _tekrarsiz_sec(havuz: list[str], kullanilan: set[str], deneme: int = 5) -> str:
    """Havuzdan, ayni fisde HENUZ KULLANILMAMIS bir aciklama secer.

    Havuzu bastan filtrelemek yerine birkac kez YENIDEN CEKER: filtreleme
    500 bin elemanli havuzda kalem basina O(n) maliyet demek, yeniden cekim
    O(1). `deneme` kadar denedikten sonra bulamazsa sonuncuyu kabul eder
    (kucuk havuzda takilip kalmamak icin).

    NEDEN GEREKLI (2026-07-30'da olculdu): eski kod havuz 500'den buyukse
    dedup'i tamamen kapatiyordu, gerekce "buyuk havuzda cakisma ~0"di. Yanlis
    cikti -- 25k fisin 409'unda (%1,64) ayni kalem iki kez vardi ('F Saff Cop
    Poseti Buyuk Boy' x2). Sebep havuzun mutlak buyuklugu degil, ESIGI YENI
    ASAN havuzlar (ofis_sarf 163 -> 4.080, icinde neredeyse ayni varyantlar)
    ve firma adi kisitinin havuzu daraltmasi."""
    for _ in range(deneme):
        aciklama = random.choice(havuz)
        if aciklama not in kullanilan:
            break
    kullanilan.add(aciklama)
    return aciklama


def rastgele_kalem(
    kalem_no: int,
    izinli_kategoriler: list[HarcamaKategorisi],
    kullanilan_aciklamalar: set[str],
    yemek_havuzu: list[str] | None = None,
    dar_havuzlar: dict[HarcamaKategorisi, list[str]] | None = None,) -> FaturaKalemi:
    """`yemek_havuzu` verilirse YEMEK_HIZMETI kalemleri genel havuz yerine ondan
    seçilir (firma adına göre mutfak kısıtı; bkz. mutfak_havuzu_sec).
    `dar_havuzlar` verilirse o kategorilerde genel havuz yerine firma adına göre
    daraltılmış havuz kullanılır (bkz. FIRMA_ADI_KISITLARI) -- kuruyemişçi yalnız
    kuruyemiş satar. İkisi farklı eksenler: mutfak kısıtı menü BÖLÜMÜ seçer,
    bu ürün ADI deseniyle süzer."""
    kategori = random.choice(izinli_kategoriler)
    _dar = (dar_havuzlar or {}).get(kategori)

    # Büyük havuzlarda (CSV kaynaklı, binlerce eleman) tekrar filtresi hem
    # gereksiz maliyetli hem de anlamsız (çakışma ihtimali zaten ~0),
    # o yüzden sadece küçük (elle yazılmış) havuzlarda filtreleme yapılır.
    BUYUK_HAVUZ_ESIGI = 500

    if _dar:
        # Firma adı kısıtı bu kategoriyi daraltmış -- TEMEL_GIDA'nın iki-havuzlu
        # yolu da dahil her şeyin önüne geçer (kuruyemişçinin gıda kalemi kuruyemiş
        # havuzundan gelmeli, market havuzundan değil).
        # Dedup BURADA EN KRITIK: kısıt havuzu daralttığı için çakışma olasılığı
        # en yüksek dal budur.
        aciklama = _tekrarsiz_sec(_dar, kullanilan_aciklamalar)
    elif kategori == HarcamaKategorisi.TEMEL_GIDA:
        # Cesitlilik icin iki ayri kaynaktan agirlikli secim:
        # %60 market_urunleri.csv, %40 temiz_urunler.csv (Supermarket etiketi).
        # (Supermarket havuzu 2026-07-30'da bosaltildi -- bkz. ilgili yorum.)
        if random.random() < TEMEL_GIDA_MARKET_AGIRLIGI and TEMEL_GIDA_MARKET_HAVUZU:
            aciklama = _tekrarsiz_sec(TEMEL_GIDA_MARKET_HAVUZU, kullanilan_aciklamalar)
        elif TEMEL_GIDA_SUPERMARKET_HAVUZU:
            aciklama = _tekrarsiz_sec(TEMEL_GIDA_SUPERMARKET_HAVUZU, kullanilan_aciklamalar)
        else:
            aciklama = _tekrarsiz_sec(TEMEL_GIDA_MARKET_HAVUZU, kullanilan_aciklamalar)
    else:
        if kategori == HarcamaKategorisi.YEMEK_HIZMETI and yemek_havuzu:
            havuz = yemek_havuzu   # mutfak kısıtı: firma adına uygun bölümler
        else:
            havuz = ACIKLAMA_HAVUZU[kategori]
        if len(havuz) > BUYUK_HAVUZ_ESIGI:
            aciklama = _tekrarsiz_sec(havuz, kullanilan_aciklamalar)
        else:
            musait_aciklamalar = [a for a in havuz if a not in kullanilan_aciklamalar]
            if not musait_aciklamalar:
                musait_aciklamalar = havuz
            aciklama = random.choice(musait_aciklamalar)
            kullanilan_aciklamalar.add(aciklama)

    # Aciklama artik belli -- birimi URUNDEN cikar (CSV eslemesi > desen >
    # rastgele). Sirala onemli: birim hem miktarin ondalikligini hem fiyat
    # katmanini belirledigi icin urun adi bilinmeden secilemez.
    birim = birim_sec(kategori, aciklama)

    return FaturaKalemi(
        kalem_no=kalem_no,
        aciklama=aciklama,
        harcama_kategorisi=kategori,
        miktar=rastgele_miktar(birim, kategori, aciklama),
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(kategori, birim, aciklama),
        iskonto_orani=0.0,   # gerçek fişlerde hiç görülmedi (2026-08-17)
        kdv_orani=kdv_orani_belirle(kategori),
    )


def rastgele_tarih(gun_araligi: int = 90) -> str:
    """Son `gun_araligi` gün içinde rastgele bir tarih üretir (gelecek tarih yok)."""
    bugun = date.today()
    rastgele_gun = random.randint(0, gun_araligi)
    tarih = bugun - timedelta(days=rastgele_gun)
    return tarih.isoformat()  # "2026-06-15" formatinda


# --- yukleme_zamani -------------------------------------------------------
# Gecikme TEK dagilimdan cekilir; temiz/anomalili ayrimi YAPILMAZ, aksi halde
# "gecikme uzunsa anomali" kestirme yolu acilir. Olculdu (24k): gecikmeden
# is_anomali'ye AUC 0,50.
YUKLEME_GECIKME_BANTLARI = [(0, 1, 40), (2, 7, 30), (8, 21, 20), (22, 45, 10)]
# Mukerrer ciftte iki yukleme arasi (gun). 0 = ayni gun, saatler farkli.
YUKLEME_ARALIK_BANTLARI = [(0, 0, 40), (1, 1, 35), (2, 3, 15), (4, 7, 10)]
YUKLEME_SAAT_AGIRLIKLARI = {
    0: 0.2, 1: 0.1, 2: 0.1, 3: 0.1, 4: 0.1, 5: 0.2, 6: 0.5, 7: 1.5,
    8: 3.0, 9: 8.0, 10: 9.5, 11: 9.0, 12: 4.5, 13: 7.0, 14: 9.0, 15: 9.0,
    16: 8.5, 17: 7.5, 18: 5.0, 19: 3.0, 20: 2.5, 21: 2.0, 22: 1.2, 23: 0.6,
}
# Tarihlerin 2/7'si hafta sonuna duser; hedef pay %12 -> %58'i is gunune kayar.
YUKLEME_HAFTA_SONU_KALMA = 0.42
TR_SAAT_DILIMI = timezone(timedelta(hours=3))


def _yukleme_bant_sec(bantlar, tavan: int) -> int:
    uygun = [(a, min(b, tavan), w) for a, b, w in bantlar if a <= tavan]
    if not uygun:
        return max(0, tavan)
    a, b, _ = random.choices(uygun, weights=[w for *_, w in uygun], k=1)[0]
    return random.randint(a, b)


def _yukleme_ani_kur(fatura_tarihi: date, gecikme: int) -> str:
    """Gecikme gunu + is saati sekillendirmesi -> ISO 8601 zaman damgasi."""
    bugun = date.today()
    gun = fatura_tarihi + timedelta(days=gecikme)
    if gun.weekday() >= 5 and random.random() >= YUKLEME_HAFTA_SONU_KALMA:
        ileri = gun + timedelta(days=7 - gun.weekday())          # Pazartesi
        geri = gun - timedelta(days=gun.weekday() - 4)           # Cuma
        gun = ileri if ileri <= bugun else (geri if geri >= fatura_tarihi else gun)
    saat = random.choices(list(YUKLEME_SAAT_AGIRLIKLARI),
                          weights=list(YUKLEME_SAAT_AGIRLIKLARI.values()), k=1)[0]
    return datetime(gun.year, gun.month, gun.day, saat, random.randrange(60),
                    tzinfo=TR_SAAT_DILIMI).isoformat()


def rastgele_yukleme_zamani(fatura_tarihi: str) -> str:
    """Fisin sisteme yuklendigi an. Fatura tarihinden sonra, bugunu asmaz."""
    tarih = date.fromisoformat(fatura_tarihi)
    tavan = max(0, (date.today() - tarih).days)
    return _yukleme_ani_kur(tarih, _yukleme_bant_sec(YUKLEME_GECIKME_BANTLARI, tavan))


YUKLEME_ASGARI_ARALIK = timedelta(hours=1)


def _gun_sonu(ornek: datetime) -> datetime:
    return datetime.combine(date.today(), time(23, 59), tzinfo=ornek.tzinfo)


def mukerrer_yukleme_zamanlari(fatura_tarihi: str, f1_zamani: str) -> tuple[str, str]:
    """Mukerrer cift icin (erken, gec) yukleme ani.

    Taban gecikme BIR KEZ cekilir, aralik taban etrafinda SIMETRIK bolunur.
    Asimetrik ekleme (f1 = taban, f2 = taban + aralik) kopyalari gecikme
    dagiliminin sagina kaydirip tek kayda bakan modele zayif bir sinyal
    birakiyordu; simetrik bolmede ciftin havuz ortalamasi yerinde kalir."""
    tarih = date.fromisoformat(fatura_tarihi)
    tavan = (date.today() - tarih).days
    if tavan < 0:
        # `gelecek_tarihli` enjekte edilmis: fatura tarihi CAPA OLAMAZ, yukleme
        # ondan ONCE olmali (anomalinin tanimi). f1'in mevcut ani korunur.
        an = datetime.fromisoformat(f1_zamani)
        gec = an + timedelta(days=_yukleme_bant_sec(YUKLEME_ARALIK_BANTLARI, 7),
                             hours=random.randint(1, 12))
        gec = min(gec, _gun_sonu(an))
    else:
        taban = _yukleme_bant_sec(YUKLEME_GECIKME_BANTLARI, tavan)
        aralik = _yukleme_bant_sec(YUKLEME_ARALIK_BANTLARI, 7)
        ikisi = sorted((
            datetime.fromisoformat(_yukleme_ani_kur(tarih, max(0, taban - aralik // 2))),
            datetime.fromisoformat(_yukleme_ani_kur(tarih, min(tavan, taban + (aralik - aralik // 2)))),
        ))
        an, gec = ikisi

    # Asgari aralik: `aralik` 0 gun cikan ciftlerde iki saat cekimi dakikalara
    # kadar yaklasabiliyor. Once kopya ileri itilir, tavana dayanirsa f1 geri cekilir.
    if gec - an < YUKLEME_ASGARI_ARALIK:
        if an + YUKLEME_ASGARI_ARALIK <= _gun_sonu(an):
            gec = an + YUKLEME_ASGARI_ARALIK
        else:
            an = gec - YUKLEME_ASGARI_ARALIK
    return an.isoformat(), gec.isoformat()


def rastgele_saat() -> str:
    """Fisin uzerindeki saat (08:00-20:59). Etiketle KORELASYONSUZ olmali:
    saati anomaliye/kategoriye baglamak gorsele sahte sinyal ekler."""
    return f"{random.randint(8, 20):02d}:{random.randrange(60):02d}"


# fis_uret.sablon_sec artik fatura_no'nun SEKLINE (FTR onekli mi) bakarak
# sablon seciyor -- format burada VERI ALANI olarak karar veriliyor, render
# katmani yalniz okuyor (bkz. fis_uret.py:sablon_sec docstring'i, 2026-08-21).
SABLON2_PAYI = 0.5   # e-arsiv (FTR formatli) olma olasiligi
YAZARKASA_FATURA_NO_USTSINIR = 999_999   # hane sayisi kisitlanmaz, 1-6 hane arasi dogal cikar

# VKN-kapsamli fatura_no kaydi: yazarkasa formatinin kucuk sayi uzayi (~1M),
# e-arsivin 9 haneli uzayina gore dogum gunu paradoksuna cok daha acik. Bu
# kayit olmasa validators.fatura_no_tekrarlarini_bul'un bugun SADECE injekte
# edilmis anomalileri bulmasi (CLAUDE.md SS7 -- dogal cakisma ~0) bozulurdu.
_VKN_FATURA_NO_KAYDI: dict[str, set[str]] = {}


def fatura_no_kaydini_sifirla() -> None:
    """Yeni bir uretim kosusu basinda cagrilir (bkz. karisik_veri_seti_uret)."""
    _VKN_FATURA_NO_KAYDI.clear()


def rastgele_fatura_no(fatura_tarihi: str, satici_vkn: str) -> str:
    """
    Fis no'yu iki formattan biriyle uretir; hangi format render katmaninda
    HANGI SABLONUN secilecegini de belirler (bkz. fis_uret.sablon_sec).
      - e-arsiv (SABLON2_PAYI olasilikla): GIB e-fatura standardina uygun 16
        hane, "FTR{yil}{9 haneli sira no, sifirla doldurulmus}".
      - yazarkasa (kalan): duz, dolgusuz sayi (ör. "35"). Gercek yazarkasalarda
        (ÖKC) fis no GİB tarafindan tanimli sabit bir genislige sahip degil,
        cihaz ömrü boyunca müteselsil artan bir sayaçtir -- GÜNLÜK SIFIRLANMAZ,
        yalniz Z raporu günlük hafizayi sifirlar (arastirildi, 2026-08-21).

    Ayni VKN icin ayni deger bir kez daha cikarsa yeniden cekilir (bkz.
    _VKN_FATURA_NO_KAYDI) -- guvenlik bu yuzden YAZARKASA_FATURA_NO_USTSINIR'in
    buyuklugune bagimli degil.
    """
    yil = fatura_tarihi[:4]   # "2026-04-20" -> "2026", tarihle tutarli olsun
    kullanilan = _VKN_FATURA_NO_KAYDI.setdefault(satici_vkn, set())
    for _ in range(1000):   # pratikte ilk denemede biter, sonsuz donguye karsi ust sinir
        if random.random() < SABLON2_PAYI:
            sira_no = random.randint(1, 999999999)
            aday = f"FTR{yil}{sira_no:09d}"   # 09d -> 9 haneye sifirla tamamla
        else:
            aday = str(random.randint(1, YAZARKASA_FATURA_NO_USTSINIR))
        if aday not in kullanilan:
            kullanilan.add(aday)
            return aday
    raise RuntimeError(
        f"fatura_no uretilemedi (VKN {satici_vkn}): 1000 denemede benzersiz "
        "deger bulunamadi -- bu VKN'nin fatura sayisi/sayi uzayi orantisiz olabilir."
    )

# Gerçek hayatta bir taksi ya da akaryakıt fişinde başka kalem OLMAZ -- fiş
# tek kalemlidir. rastgele_fatura() bu açiklamalardan biri kalem olarak
# seçildiğinde, o kalem faturanin TEK kalemi olacak şekilde döngüyü keser.
TEKIL_ZORUNLU_ACIKLAMALAR = {
    "Taksi Ücreti", "Şehir İçi Taksi Ücreti", "Havalimanı Taksi Ücreti",
    "Otopark Ücreti", "Kapalı Otopark Ücreti", "Havalimanı Otopark Ücreti",
    "Yakit Gideri", "Vale Hizmeti",
}


# İş kolu başına kalem sayısı TAVANI.
#
# NEDEN: kalem sayısı eskiden `randint(1, 8)` ile HER iş kolunda aynıydı --
# ölçüldü, 100k'da her sektörün ortalaması 4,56-4,60 ve faturaların ~%63'ü
# 4+ kalemliydi. Bu, hizmet sektörlerinde gerçek dışı fişler üretiyordu:
# tek fişte "Feribot Bileti + İstanbulkart + LPG + Metro + Uçak Bileti" ya da
# bir danışmanlık faturasında altı ayrı hizmet kalemi. Gerçek hayatta feribota
# binersin, fişi ayrıdır; akbil yüklersin, fişi ayrıdır.
#
# Faz B'ye ETKİSİ (asıl gerekçe): açıklama üretiminde modelin tek cümlede
# uzlaştırması gereken heterojen kalem sayısı düşer -- halüsinasyon yüzeyi ve
# uzunluk baskısı azalır. Pilotta gözlenen "araç kiraladım" (4 farklı ulaşım
# kalemi varken) tipi eksik/yanlış özetlemelerin kaynağı buydu.
#
# TEKIL_ZORUNLU_ACIKLAMALAR bu mekanizmanın KALEM düzeyindeki dar hâliydi
# (yalnız taksi/yakıt); burası iş kolu düzeyine genelleştirir, ikisi birlikte
# çalışır (tekil zorunlu kalem seçilirse tavan ne olursa olsun fatura 1 kaleme
# iner).
#
# Tavanı YÜKSEK kalan üçlü bilinçlidir: market sepeti, grup yemeği ve
# kırtasiye toplu alımı gerçekten çok kalemlidir. Otel folyosu da doğal olarak
# birkaç satırdır (konaklama + kahvaltı + geç çıkış), o yüzden orta seviyede.
IS_KOLU_KALEM_TAVANI: dict[IsKolu, int] = {
    IsKolu.ULASIM_SAGLAYICI: 1,      # taksi/feribot/akbil/yakıt: tek olay, tek fiş
    IsKolu.DANISMANLIK_FIRMASI: 2,   # bir sözleşme = bir hizmet kalemi
    IsKolu.LOJISTIK_FIRMASI: 2,      # tek sevkiyat/kargo
    IsKolu.TEKNOLOJI: 3,             # cihaz + lisans makul
    IsKolu.ORGANIZASYON: 4,
    IsKolu.OTEL: 5,                  # folyo doğal olarak çok satırlı
    IsKolu.RESTORAN: 8,              # grup yemeği
    IsKolu.MARKET: 8,                # market sepeti
    IsKolu.OFIS_TEDARIK: 8,          # kırtasiye toplu alım
}

VARSAYILAN_KALEM_TAVANI = 8


# --- Firma Registry (tek kalıcı firma-kimliği kaynağı) ---
# Firma kişiliği artık FATURA bazlı DEĞİL, FIRMA bazlıdır: ad + kimlik no +
# is_kolu + firma_türü tek bir registry'de (data/firma_registry.csv) DONAR ve
# fatura üretimi bu registry'den bir firma SEÇER (icat etmez). Bu sayede aynı
# ad→hep aynı VKN, aynı VKN→hep aynı ad (tutarsızlık yapısal olarak imkânsız).
# Üretimi: firma_registry_olustur.py.
FIRMA_REGISTRY_CSV = Path(__file__).parent.parent.parent / "data" / "firma_registry.csv"

# LAZY yükleme: import anında DEĞİL, ilk kullanımda bir kez okunur ve cache'lenir.
# (firma_registry_olustur.py bu modülü import ettiği için, CSV henüz YOKken
# import-anı yükleme tavuk-yumurta kilidine yol açardı.)
_FIRMA_REGISTRY: dict[IsKolu, list[dict]] | None = None


def firma_registry_yukle() -> dict[IsKolu, list[dict]]:
    """CSV'yi is_kolu -> [{'unvan':..., 'kimlik':...}, ...] olarak yükler."""
    import csv as _csv
    if not FIRMA_REGISTRY_CSV.exists():
        raise FileNotFoundError(
            f"Firma registry bulunamadı: {FIRMA_REGISTRY_CSV}\n"
            f"Önce registry'yi üret: python -m faz_a_fis_uretimi.firma_registry_olustur"
        )
    gruplar: dict[IsKolu, list[dict]] = {}
    atlanan: dict[str, int] = {}
    with open(FIRMA_REGISTRY_CSV, encoding="utf-8") as f:
        for satir in _csv.DictReader(f):
            try:
                is_kolu = IsKolu(satir["is_kolu"])
            except ValueError:
                # Şemadan kaldırılmış iş kolu (giyim_magazasi / kisisel_bakim,
                # 2026-08-14). Registry yeniden üretilene kadar bu satırlar
                # okunur ama kullanılmaz; sessiz düşmesin diye sayılır.
                atlanan[satir["is_kolu"]] = atlanan.get(satir["is_kolu"], 0) + 1
                continue
            gruplar.setdefault(is_kolu, []).append({
                "unvan": satir["satici_unvan"],
                "kimlik": satir["satici_kimlik"],
                # Eski registry'de kolon yok -> bos (sahis adi basilmaz, kirilmaz).
                "sahis_adi": (satir.get("sahis_adi") or "").strip(),
                "adres": (satir.get("adres") or "").strip(),
            })
    if atlanan:
        import warnings
        warnings.warn(
            f"\n  Registry'de şemada olmayan iş kolu: {atlanan}"
            f"\n  -> bu firmalar kullanılmıyor. Temizlemek için: "
            f"python -m faz_a_fis_uretimi.firma_registry_olustur",
            RuntimeWarning, stacklevel=2,
        )
    return gruplar


def registry_firma_sec(is_kolu: IsKolu) -> dict:
    """Verilen iş kolundaki registry firmalarından birini rastgele seçer."""
    global _FIRMA_REGISTRY
    if _FIRMA_REGISTRY is None:
        _FIRMA_REGISTRY = firma_registry_yukle()
    firmalar = _FIRMA_REGISTRY.get(is_kolu)
    if not firmalar:
        raise ValueError(f"Registry'de '{is_kolu.value}' iş kolu için firma yok.")
    return random.choice(firmalar)


def rastgele_fatura() -> Fatura:
    # 1. adim: iş kolunu (fatura dağılımını) ağırlıklı seç -- mevcut dağılım korunur.
    is_kolu = random.choices(
        list(IS_KOLU_AGIRLIKLARI.keys()),
        weights=list(IS_KOLU_AGIRLIKLARI.values()),
        k=1,
    )[0]
    izinli_kategoriler = IS_KOLU_KATEGORILERI[is_kolu]

    # 2. adim: firma KİMLİĞİNİ registry'den SEÇ (ad + kimlik no sabit, fatura başına
    # üretilmez). Sektör-kelime/geri-okuma mekanizması emekli oldu; is_kolu artık
    # Fatura'da açıkça saklanıyor.
    firma = registry_firma_sec(is_kolu)
    satici_kimlik = firma["kimlik"]
    satici_adi = firma["unvan"]
    satici_sahis_adi = firma.get("sahis_adi", "")
    satici_adres = firma.get("adres", "")

    # 2b. Mutfak kısıtı: firma adı dar bir mutfağı işaret ediyorsa (çiğköfteci,
    # balıkçı, pizzacı...) yemek kalemleri o mutfağın menüsünden seçilir.
    # Geniş mutfaklarda (kebap/lokanta/ocakbaşı) None döner, kısıt uygulanmaz.
    yemek_havuzu = mutfak_havuzu_sec(satici_adi)

    # 2c. Firma adi kisiti (restoran DISI iş kolları): ad is_kolu'ndan daha dar bir
    # yelpaze işaret ediyorsa izinli kategoriler ve/veya kalem havuzları daraltılır
    # (bkz. FIRMA_ADI_KISITLARI). Eşleşme yoksa mevcut davranış korunur.
    dar_havuzlar: dict[HarcamaKategorisi, list[str]] = {}
    _kisit = firma_adi_kisiti_sec(is_kolu, satici_adi)
    if _kisit is not None:
        izinli_kategoriler, dar_havuzlar = _kisit
    # 2d. NEGATIF kisit: kucuk havuzlarda (ulasim_bireysel 23 urun) pozitif kisit
    # ASGARI_ALT_HAVUZ esigini gecemedigi icin calismaz -- orada imkansiz urunu
    # eleriz (bkz. FIRMA_ADI_YASAK_URUN). Pozitif kisit zaten daralttiysa ona
    # DOKUNMAZ, yalniz bos kalan kategorileri doldurur.
    for _kat, _havuz in firma_yasak_urun_havuzlari(is_kolu, satici_adi).items():
        dar_havuzlar.setdefault(_kat, _havuz)

    fatura_tarihi = rastgele_tarih()
    fatura_no = rastgele_fatura_no(fatura_tarihi, satici_kimlik)

    # Bu iş kolunda toplam kaç benzersiz açiklama üretilebilir? artık csv dosyalarından verileri çekildiği için bu gereksiz olabilir
    toplam_musait_aciklama = sum(
        len(ACIKLAMA_HAVUZU[kategori]) for kategori in izinli_kategoriler
    )

    # Kalem sayisi: iş koluna göre TAVAN (bkz. IS_KOLU_KALEM_TAVANI gerekçesi)
    # ve havuz çeşitliliği -- ikisinden küçüğü.
    is_kolu_tavani = IS_KOLU_KALEM_TAVANI.get(is_kolu, VARSAYILAN_KALEM_TAVANI)
    ust_sinir = min(is_kolu_tavani, toplam_musait_aciklama)
    kalem_sayisi = random.randint(1, max(1, ust_sinir))

    kullanilan_aciklamalar: set[str] = set()
    kalemler: list[FaturaKalemi] = []
    for i in range(kalem_sayisi):
        kalem = rastgele_kalem(i + 1, izinli_kategoriler, kullanilan_aciklamalar,
                               yemek_havuzu, dar_havuzlar)
        if kalem.aciklama in TEKIL_ZORUNLU_ACIKLAMALAR:
            # Bu kalem seçildiği an, öncekiler dahil hepsini atip faturayi
            # TEK kalemli yapiyoruz (taksi/yakit fişi başka kalemle gelmez).
            kalemler = [kalem.model_copy(update={"kalem_no": 1})]
            break
        kalemler.append(kalem)

    return Fatura(
        fatura_no=fatura_no,
        fatura_tarihi=fatura_tarihi,
        yukleme_zamani=rastgele_yukleme_zamani(fatura_tarihi),
        saat=rastgele_saat(),
        satici_vkn=satici_kimlik,
        satici_unvan=satici_adi,
        adres=satici_adres,
        satici_sahis_adi=satici_sahis_adi,
        is_kolu=is_kolu,   # yeni
        kalemler=kalemler,
    )

def kimlik_etiketi(kimlik_no: str) -> str:
    """10 haneliyse VKN, 11 haneliyse TCKN etiketi döndürür."""
    if len(kimlik_no) == 11:
        return "TCKN"
    return "VKN"


if __name__ == "__main__":
    fatura = rastgele_fatura()

    print("=" * 60)
    print(f"  FATURA NO   : {fatura.fatura_no}")
    print(f"  TARİH       : {fatura.fatura_tarihi}")
    print(f"  SATICI      : {fatura.satici_unvan}")
    etiket = kimlik_etiketi(fatura.satici_vkn)
    print(f"  SATICI {etiket:<4} : {fatura.satici_vkn}")
    print("=" * 60)

    for k in fatura.kalemler:
        print(f"\n  [{k.kalem_no}] {k.aciklama}  ({k.harcama_kategorisi.value})")
        print(f"      Miktar       : {k.miktar} {k.birim}")
        print(f"      Birim Fiyat  : {k.birim_fiyat} TL")
        print(f"      İskonto      : %{k.iskonto_orani}")
        print(f"      KDV Orani    : %{k.kdv_orani}")
        print(f"      Ara Toplam   : {k.ara_toplam:.2f} TL")
        print(f"      KDV Tutari   : {k.kdv_tutari:.2f} TL")
        print(f"      Satir Toplam : {k.satir_toplam:.2f} TL")

    print("\n" + "=" * 60)
    print(f"  GENEL TOPLAM: {fatura.genel_toplam:.2f} TL")
    print(f"  VERGİSİZ TOPLAM: {fatura.toplam_vergisiz_tutar:.2f} TL")
    print(f"  KDV TOPLAM: {fatura.toplam_kdv_tutari:.2f} TL")
    print(f"  İSKONTO TOPLAM: {fatura.toplam_iskonto:.2f} TL")
    print("=" * 60)