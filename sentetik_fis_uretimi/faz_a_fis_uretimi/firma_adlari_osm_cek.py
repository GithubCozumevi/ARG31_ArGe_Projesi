"""
OpenStreetMap (Overpass API) üzerinden Türkiye'deki gerçek işletme adlarını
is_kolu kategorilerine göre çeker, tek bir CSV'ye yazar. OSM verisi ODbL ile
lisanslı -- toplu çekim/yeniden kullanım (atıfla) açıkça izin verilen bir
kullanım, Google/Yandex Maps'in ToS'unun aksine (bkz. https://www.openstreetmap.org/copyright).

ŞEHİR KUTULARI + ŞEHİR BAŞI KOTA:
Önceki sürüm tüm Türkiye bbox'ını kaba grid'e bölüp tarıyordu. İki sorunu vardı:
(1) dikdörtgen Halep/Musul/Erbil/Rodos/Yerevan'ı da kapsadığı için çekilen adların
%22'si (market'te %45'i) Türkiye dışıydı -- Arapça/Yunanca isimler bu yüzden geliyordu;
(2) grid'in büyük kısmı deniz ve boş step olduğundan istekler boşa gidiyordu.
Bu sürüm YALNIZCA sınırlardan uzak, büyükten küçüğe sıralı ~55 şehir kutusunu tarar.

COĞRAFİ DAĞILIM ÖNEMLİ DEĞİL (bilinçli karar): hedef, adların illere yayılması
değil, iş koluna uygun Türkçe firma adı HACMİNİ hızlı toplamak. Hepsi İstanbul'dan
gelse de olur. Bu yüzden şehir başı kota varsayılan olarak KAPALI (--kutu-kota 0);
hedef hangi kutudan dolarsa oradan dolar, dolunca tarama biter. Yayılım istenirse
--kutu-kota 100-200 ile açılabilir (yavaşlatır).

YABANCI İSİM KORUMASI iki katmanlı: kutuların sınırlardan uzak seçilmesi +
latin_disi_mi() süzgeci. Overpass'ın area(TR) filtresi DENENDİ ve BIRAKILDI --
İstanbul kutusunda 93 sn'de timeout verirken filtresiz sorgu 57 sn'de dönüyor
(Türkiye sınır poligonu karmaşık, node başına kesişim testi pahalı).

ÜÇ MOD:
  1. Tam tarama (varsayılan): şehir kutularını gezip YENİ firma adı toplar.
  2. `--etiket-tazele`: yeni firma aramaz. Mevcut CSV'deki `osm_id`'lerin
     etiketlerini `node(id:...)` ile çeker (18 bin id ~19 istek) ve ham etiket
     + alt tip kolonlarıyla AYRI dosyaya yazar. Firma kümesi değişmediği için
     firma_registry.csv'nin yeniden üretilmesi gerekmez.
  3. `--yeniden-etiketle`: ağa hiç çıkmaz, alt_tip'i ham etiketten yeniden türetir.

HAM ETİKET NEDEN SAKLANIYOR: `alt_tip` bir YORUM, `osm_anahtar/osm_deger` ise
ham gerçek. Sözlük yanlış çıkarsa (2026-08-13'te `cuisine` böyle oldu) yorumu
saniyeler içinde yeniden üretiriz; ham gerçeği yeniden üretmek Overpass koşusu
demek.

Kullanım:
    python -m faz_a_fis_uretimi.firma_adlari_osm_cek                       # varsayılan hedef 3000/is_kolu
    python -m faz_a_fis_uretimi.firma_adlari_osm_cek --hedef 3000 --bekleme 10
    # Daha hızlı/kaba tarama için yalnızca en büyük N şehir:
    python -m faz_a_fis_uretimi.firma_adlari_osm_cek --sehir-limit 25
    # Yalnız alt tip: registry'deki firmaların etiketlerini tazele
    python -m faz_a_fis_uretimi.firma_adlari_osm_cek --etiket-tazele --bekleme 5
    # Sözlük değişti, veriyi yeniden yorumla (ağ yok)
    python -m faz_a_fis_uretimi.firma_adlari_osm_cek --yeniden-etiketle --kaynak data/companies/firma_adlari_osm_alt_tip.csv
"""

import argparse
import csv
import re
import time
from pathlib import Path

import requests

# Overpass sunucuları. Ana sunucu (overpass-api.de) IP başına 2 slot + biriken
# sorgu süresi kotası uyguluyor; uzun çekimlerde kalıcı 429'a düşüyor. Aynalar
# ayrı kotaya sahip -> ana sunucu tıkandığında --sunucu ile geçilir.
OVERPASS_SUNUCULAR = {
    "ana": "https://overpass-api.de/api/interpreter",
    "kumi": "https://overpass.kumi.systems/api/interpreter",
    "coffee": "https://overpass.private.coffee/api/interpreter",
}
OVERPASS_URL = OVERPASS_SUNUCULAR["ana"]
# overpass-api.de varsayılan requests User-Agent'ını reddediyor (406) -- açıkça
# kimliklenen bir User-Agent göndermek gerekiyor.
ISTEK_BASLIKLARI = {"User-Agent": "masrafAI-veri-cikarma/1.0 (egitim amacli sentetik veri projesi)"}
CIKTI_CSV = Path("data/companies/firma_adlari_osm.csv")

# is_kolu -> [(osm anahtar, osm değer, alt_tip), ...]
# schema.py:IS_KOLU_KATEGORILERI ile aynı 11 iş kolu.
#
# alt_tip, satıcının hangi ürünleri satabileceğinin TEK kaynağıdır: eskiden firma
# ADINDAN regex ile tahmin ediliyordu (field_generator.FIRMA_ADI_KISITLARI), oysa
# satırı üreten OSM etiketi bunu zaten kesin biliyor. Yeni etiket eklerken alt_tip
# adını data/urun_satici_uyumu.csv'deki sözlükle AYNI yaz.
IS_KOLU_OSM_ETIKETLERI: dict[str, list[tuple[str, str, str]]] = {
    # Mutfak ayrimi MUTFAK_KISITLARI'nda; burada yalniz menu bolumu OLAN alt tipler.
    "restoran": [("amenity", "restaurant", "restoran"),
                 ("amenity", "fast_food", "fast_food"),
                 ("amenity", "cafe", "pastane"),
                 ("amenity", "ice_cream", "pastane"),
                 ("shop", "pastry", "pastane"),
                 ("shop", "bakery", "restoran"),
                 ("amenity", "bar", "restoran"),
                 ("amenity", "pub", "restoran")],
    "market": [("shop", "supermarket", "supermarket"),
               ("shop", "convenience", "bakkal"),
               ("shop", "butcher", "kasap"),
               ("shop", "greengrocer", "manav"),
               ("shop", "confectionery", "sekerci"),
               ("shop", "kiosk", "bufe"),
               ("shop", "alcohol", "tekel"),
               ("shop", "tobacco", "tutuncu"),
               ("shop", "seafood", "balikci")],
    "otel": [("tourism", "hotel", "otel"),
             ("tourism", "guest_house", "pansiyon"),
             ("tourism", "hostel", "hostel")],
    "ofis_tedarik": [("shop", "stationery", "kirtasiye"),
                     ("shop", "furniture", "mobilya"),
                     ("shop", "books", "kitabevi"),
                     ("shop", "copyshop", "fotokopi")],
    "teknoloji": [("shop", "electronics", "elektronik"),
                  ("shop", "computer", "bilgisayar"),
                  ("shop", "mobile_phone", "telefon"),
                  ("shop", "hifi", "ses")],
    "danismanlik_firmasi": [("office", "consulting", "danismanlik"),
                            ("office", "it", "bilisim"),
                            ("office", "accountant", "muhasebe"),
                            ("office", "tax_advisor", "mali_musavir"),
                            ("office", "lawyer", "hukuk"),
                            ("office", "advertising_agency", "reklam")],
    "lojistik_firmasi": [("office", "logistics", "lojistik"),
                         ("office", "courier", "kargo"),
                         ("amenity", "post_office", "postane")],
    "ulasim_saglayici": [("amenity", "fuel", "akaryakit"),
                         ("amenity", "taxi", "taksi"),
                         ("amenity", "car_rental", "arac_kiralama"),
                         ("shop", "travel_agency", "seyahat_acentesi"),
                         ("shop", "car_repair", "oto_servis"),
                         ("shop", "tyres", "lastikci"),
                         ("amenity", "car_wash", "arac_yikama")],
    "organizasyon": [("office", "event_management", "ajans"),
                     ("amenity", "events_venue", "mekan"),
                     ("amenity", "conference_centre", "kongre")],
    "giyim_magazasi": [("shop", "clothes", "giyim"),
                       ("shop", "shoes", "ayakkabi"),
                       ("shop", "bag", "canta"),
                       ("shop", "tailor", "terzi")],
    "kisisel_bakim": [("shop", "hairdresser", "kuafor"),
                      ("shop", "beauty", "guzellik"),
                      ("shop", "cosmetics", "kozmetik"),
                      ("shop", "chemist", "drogeri"),
                      ("shop", "perfumery", "parfumeri")],
}

# OSM `cuisine` denendi, GERI ALINDI (CLAUDE.md 2026-08-13). Bos birakiliyor.
IS_KOLU_CUISINE_ALT_TIPLERI: dict[str, dict[str, str]] = {}

VARSAYILAN_ALT_TIP = "genel"
ALT_TIP_TABANI = 40   # bunun altinda kalan alt tip raporda isaretlenir (dar havuz -> leakage)

# --- Alt tip yasam kurallari ------------------------------------------------
#
# Bir alt tip ancak URUN tarafinda ayristirilabilir bir havuzu varsa ANLAM
# tasir. Havuzu olmayan alt tip uretimde sessizce bos kalir ya da rastgele urun
# cektirir (2026-08-13'te `firin`/`bar` boyle uretildi).
#
# ERITME  : alt tip baska bir alt tipin davranisini aynen aliyorsa ona indirgenir.
# BEKLEME : havuzu ileride etiketlenecekse alt_tip kolonuna `genel` yazilir.
#           HAM ETIKET (osm_anahtar/osm_deger) her halukarda saklandigi icin
#           urun tarafi hazir olunca `--yeniden-etiketle` yeterli, yeniden
#           cekim GEREKMEZ.
ALT_TIP_ERITME: dict[str, str] = {
    "tutuncu": "bufe",       # sigara yasakli kategori -> temiz uretimde havuzu yok
    "tekel": "bufe",         # ayni gerekce (alkol yasakli); ikisi de icecek/atistirmalik satar
    "kitabevi": "kirtasiye",  # hicbir kategoride kitap yok
    "fotokopi": "kirtasiye",  # urun degil hizmet
    "ses": "elektronik",     # 6 firma
    "terzi": "giyim",        # havuzu yok
}

# Urun tarafi HAZIR olan alt tipler. Digerleri `genel` yazilir.
#   restoran/fast_food/pastane -> restoran_urunleri.csv bolumleri
#   kasap/manav                -> market CSV'de ET TAVUK / MEYVE SEBZE deterministik
#   mobilya/kirtasiye/bilisim  -> kategori duzeyinde ayrisiyor, urun etiketi gerekmiyor
AKTIF_ALT_TIPLER: set[str] = {
    "restoran", "fast_food", "pastane",
    "kasap", "manav",
    "mobilya", "kirtasiye", "bilisim",
}

# (osm anahtar, osm deger) -> ham alt tip. Sorgu tablosundan turetilir.
_ETIKET_ALT_TIP: dict[tuple[str, str], str] = {
    (anahtar, deger): alt_tip
    for etiketler in IS_KOLU_OSM_ETIKETLERI.values()
    for anahtar, deger, alt_tip in etiketler
}


def sorgu_satirlari(etiketler: list[tuple[str, str, str]]) -> list[str]:
    return [f'node["{anahtar}"="{deger}"]' for anahtar, deger, _ in etiketler]


def ham_etiket_coz(osm_etiketleri: dict,
                   etiketler: list[tuple[str, str, str]]) -> tuple[str, str, str]:
    """Elemanin HAM OSM etiketini bulur: (anahtar, deger, ham alt tip).
    Sira onemli, ilk eslesen kazanir (bir node hem amenity=cafe hem shop=bakery
    tasiyabilir). Eslesme yoksa ucu de bos/genel."""
    for anahtar, deger, alt_tip in etiketler:
        if osm_etiketleri.get(anahtar) == deger:
            return anahtar, deger, alt_tip
    return "", "", VARSAYILAN_ALT_TIP


def alt_tip_uygula(ham_alt_tip: str) -> str:
    """Ham alt tipe eritme ve bekleme kurallarini uygular."""
    alt_tip = ALT_TIP_ERITME.get(ham_alt_tip, ham_alt_tip)
    return alt_tip if alt_tip in AKTIF_ALT_TIPLER else VARSAYILAN_ALT_TIP


def satirdan_alt_tip(satir: dict) -> str:
    """CSV satirindaki HAM etiketten alt tipi yeniden turetir (ag gerekmez)."""
    anahtar = (satir.get("osm_anahtar") or "").strip()
    deger = (satir.get("osm_deger") or "").strip()
    return alt_tip_uygula(_ETIKET_ALT_TIP.get((anahtar, deger), VARSAYILAN_ALT_TIP))

# Türkiye'nin OSM alan id'si (relation 174737 + 3600000000). ŞU AN KULLANILMIYOR:
# sorguya area kısıtı eklemek doğru sonucu veriyor ama İstanbul gibi yoğun
# kutularda timeout'a yol açtı (bkz. sorgu_olustur docstring). Referans için
# duruyor -- tekrar denenecekse önce maliyeti ölç.
TURKIYE_AREA_ID = 3600174737

# Taranacak şehir kutuları: (ad, lat, lon, dlat, dlon) -> bbox = lat±dlat, lon±dlon.
# Tüm Türkiye'yi grid'lemek yerine YALNIZCA şehirler taranır; üç kazanç:
#   1) Akdeniz/Karadeniz/boş step üzerinde istek harcanmaz (grid'in çoğu boştu).
#   2) Kutular küçük -> sorgular hafif, 504/bölme neredeyse hiç olmaz.
#   3) Kutular sınırlardan uzak seçildi; latin_disi_mi() de ikinci güvence.
# Sıra ÖNEMLİ: büyük şehirler başta, şehir başı kota dolunca sıradakine geçilir.
# Sınıra çok yakın iller (Hatay, Edirne, Mardin, Kilis, Iğdır) bilerek DIŞARIDA.
SEHIR_KUTULARI = [
    ("istanbul", 41.03, 28.95, 0.28, 0.75),
    ("ankara", 39.93, 32.86, 0.25, 0.35),
    ("izmir", 38.42, 27.15, 0.25, 0.30),
    ("bursa", 40.19, 29.06, 0.20, 0.30),
    ("antalya", 36.89, 30.71, 0.20, 0.35),
    ("adana", 37.00, 35.32, 0.18, 0.30),
    ("konya", 37.87, 32.48, 0.20, 0.30),
    ("gaziantep", 37.07, 37.38, 0.15, 0.25),
    ("mersin", 36.80, 34.63, 0.15, 0.30),
    ("kayseri", 38.73, 35.49, 0.18, 0.28),
    ("eskisehir", 39.78, 30.52, 0.18, 0.28),
    ("kocaeli", 40.77, 29.92, 0.18, 0.35),
    ("samsun", 41.29, 36.33, 0.15, 0.30),
    ("denizli", 37.78, 29.09, 0.18, 0.28),
    ("sanliurfa", 37.16, 38.80, 0.15, 0.25),
    ("diyarbakir", 37.91, 40.24, 0.18, 0.28),
    ("malatya", 38.35, 38.32, 0.18, 0.28),
    ("kahramanmaras", 37.58, 36.93, 0.18, 0.28),
    ("erzurum", 39.90, 41.27, 0.18, 0.28),
    ("manisa", 38.61, 27.43, 0.18, 0.28),
    ("balikesir", 39.65, 27.89, 0.20, 0.30),
    ("tekirdag", 40.98, 27.51, 0.18, 0.30),
    ("trabzon", 41.00, 39.72, 0.12, 0.28),
    ("aydin", 37.85, 27.84, 0.15, 0.28),
    ("sakarya", 40.78, 30.40, 0.18, 0.28),
    ("mugla", 37.21, 28.36, 0.15, 0.25),
    ("sivas", 39.75, 37.02, 0.18, 0.28),
    ("elazig", 38.68, 39.22, 0.15, 0.25),
    ("van", 38.49, 43.38, 0.18, 0.28),
    ("afyonkarahisar", 38.76, 30.54, 0.18, 0.28),
    ("batman", 37.88, 41.13, 0.15, 0.25),
    ("ordu", 40.98, 37.88, 0.12, 0.28),
    ("tokat", 40.31, 36.55, 0.18, 0.28),
    ("corum", 40.55, 34.95, 0.18, 0.28),
    ("zonguldak", 41.45, 31.79, 0.12, 0.28),
    ("kutahya", 39.42, 29.98, 0.18, 0.28),
    ("isparta", 37.76, 30.55, 0.18, 0.28),
    ("osmaniye", 37.07, 36.25, 0.15, 0.25),
    ("canakkale", 40.15, 26.41, 0.15, 0.25),
    ("giresun", 40.91, 38.39, 0.12, 0.25),
    ("bolu", 40.74, 31.61, 0.18, 0.28),
    ("usak", 38.68, 29.41, 0.18, 0.28),
    ("nevsehir", 38.62, 34.71, 0.18, 0.28),
    ("aksaray", 38.37, 34.03, 0.18, 0.28),
    ("karaman", 37.18, 33.22, 0.18, 0.28),
    ("nigde", 37.97, 34.68, 0.18, 0.28),
    ("kirikkale", 39.85, 33.51, 0.15, 0.25),
    ("yozgat", 39.82, 34.81, 0.18, 0.28),
    ("amasya", 40.65, 35.83, 0.15, 0.25),
    ("kastamonu", 41.38, 33.78, 0.15, 0.28),
    ("duzce", 40.84, 31.16, 0.15, 0.25),
    ("burdur", 37.72, 30.29, 0.15, 0.25),
    ("erzincan", 39.75, 39.49, 0.15, 0.25),
    ("adiyaman", 37.76, 38.28, 0.15, 0.25),
]

# --- Adaptif bölme parametreleri (CLI'dan ezilebilir) ---
VARSAYILAN_HEDEF = 3000        # is_kolu başına toplanmaya çalışılacak benzersiz isim
VARSAYILAN_BEKLEME = 10        # istekler arası saniye (ücretsiz sunucuya nezaket)
MAX_DERINLIK = 4               # bir tile en fazla kaç kez 4'e bölünsün (504 halinde)
PER_TILE_TIMEOUT = 100         # Overpass sorgu timeout'u (sn)
HTTP_TIMEOUT = 130             # requests istek timeout'u (sn), Overpass timeout'undan büyük
RATE_LIMIT_BEKLEME = 30        # 429 (Too Many Requests) sonrası ek bekleme
MAX_THROTTLE_DENEME = 4        # 429'da aynı tile kaç kez (üstel geri çekilmeyle) yeniden denensin

# _istek dönüş sinyalleri: liste -> başarı; aşağıdakiler -> başarısız ama SEBEBİ farklı.
BOL = "bol"      # gerçek timeout (sorgu ağır) -> tile'ı 4'e bölmek mantıklı
BEKLE = "bekle"  # rate limit -> bölmek yükü ARTIRIR; aynı tile'ı bekleyip tekrar dene


def sorgu_olustur(etiketler: list[str], bbox: tuple, limit: int) -> str:
    """Düz bbox sorgusu -- area(TR) filtresi BİLEREK kullanılmıyor.
    Ölçüldü (istanbul kutusu, market tagleri): area(TR) ile 93 sn -> timeout,
    filtresiz 57 sn -> 6307 eleman. Türkiye sınır poligonu karmaşık olduğundan
    her node için kesişim testi sorguyu timeout'a itiyor. Yabancı isim koruması
    artık iki katmanda: (1) SEHIR_KUTULARI sınırlardan uzak seçildi,
    (2) latin_disi_mi() ile alfabe bazlı son süzgeç."""
    g, b, k, d = bbox
    bbox_str = f"{g},{b},{k},{d}"
    govde = "\n  ".join(f"{e}({bbox_str});" for e in etiketler)
    return f"""
[out:json][timeout:{PER_TILE_TIMEOUT}];
(
  {govde}
);
out {limit};
""".strip()


# Arapça / Ermenice / Gürcüce / Yunanca / Kiril -- bir kutu sınırı aşarsa veya
# Türkiye içindeki yabancı-alfabe tabelalar gelirse son süzgeç.
LATIN_DISI = re.compile(r"[؀-ۿݐ-ݿ԰-֏Ⴀ-ჿ"
                        r"Ͱ-ϿЀ-ӿ֐-׿]")


def latin_disi_mi(ad: str) -> bool:
    return bool(LATIN_DISI.search(ad))


def bbox_dorte_bol(bbox: tuple) -> list[tuple]:
    """Bir bbox'ı (güney,batı,kuzey,doğu) 2x2 dört alt-bbox'a böler."""
    g, b, k, d = bbox
    orta_lat = (g + k) / 2
    orta_lon = (b + d) / 2
    return [
        (g, b, orta_lat, orta_lon),
        (g, orta_lon, orta_lat, d),
        (orta_lat, b, k, orta_lon),
        (orta_lat, orta_lon, k, d),
    ]


def sehir_gridi() -> list[tuple]:
    """SEHIR_KUTULARI'nı (güney,batı,kuzey,doğu) bbox listesine çevirir."""
    return [(lat - dlat, lon - dlon, lat + dlat, lon + dlon)
            for _, lat, lon, dlat, dlon in SEHIR_KUTULARI]


class Cekici:
    def __init__(self, hedef: int, bekleme: int, kutu_kota: int,
                 sunucu: str = OVERPASS_URL, alt_tip_tavan: int = 0):
        self.hedef = hedef
        self.bekleme = bekleme
        self.sunucu = sunucu
        # Alt tip başına azami isim; 0 = SINIRSIZ. Market gibi bol etiketli iş
        # kollarında supermarket'in kasap/manav'ı ezmesini engeller.
        self.alt_tip_tavan = alt_tip_tavan
        # Şehir başına azami isim; 0 = SINIRSIZ (varsayılan). Sınırsızken hedef
        # nereden dolarsa oradan dolar -- çoğu kategoride İstanbul+Ankara yeter.
        # Amaç coğrafi denge DEĞİL, iş koluna uygun Türkçe ad hacmini hızlı
        # toplamak. Coğrafi yayılım istenirse --kutu-kota ile açılır.
        self.kutu_kota = kutu_kota
        self.istek_sayaci = 0
        self.kesildi = False   # Ctrl+C ile yarıda kesildi mi (kısmi kayıt işareti)

    def _istek(self, sorgu: str) -> list[dict] | str:
        """Tek bir Overpass sorgusu. Timeout/504 -> BOL, 429 -> BEKLE; başka hata -> raise."""
        self.istek_sayaci += 1
        try:
            yanit = requests.post(self.sunucu, data={"data": sorgu},
                                  headers=ISTEK_BASLIKLARI, timeout=HTTP_TIMEOUT)
            if yanit.status_code == 429:
                return BEKLE  # rate limit -> bölme, bekle ve aynı tile'ı tekrar dene
            if yanit.status_code in (504, 502, 503):
                # DİKKAT: overpass-api.de bu kodu İKİ ayrı sebeple döndürüyor ve
                # ayrımı yalnızca gövde metni veriyor:
                #   "Query timed out"    -> sorgu gerçekten ağır, BÖLMEK doğru
                #   "too busy"/Dispatcher-> sunucu meşgul, bölmek İŞE YARAMAZ
                # Ayrım yapılmazsa meşgul sunucuda 3 km'lik tile'lar bile timeout
                # verip derinlik tavanına kadar bölünüyor (tile başına ~341 istek).
                return BOL if "Query timed out" in yanit.text else BEKLE
            yanit.raise_for_status()
            veri = yanit.json()
            # Overpass ağır sorguda HTTP 200 + remark ile de dönebiliyor.
            if "timed out" in str(veri.get("remark", "")):
                return BOL
            return veri.get("elements", [])
        except requests.exceptions.Timeout:
            return BOL        # istemci tarafı timeout -> tile'ı böl
        finally:
            time.sleep(self.bekleme)

    def _istek_dayanikli(self, sorgu: str) -> list[dict] | None:
        """429'da AYNI sorguyu üstel geri çekilmeyle tekrar dener (bölmez -- bölmek
        tam da sunucunun istemediği şeyi, istek sayısını 4'e katlamayı yapar).
        None -> gerçek timeout, çağıran tile'ı bölmeli."""
        bekleme = RATE_LIMIT_BEKLEME
        for deneme in range(1, MAX_THROTTLE_DENEME + 1):
            sonuc = self._istek(sorgu)
            if isinstance(sonuc, list):
                return sonuc
            if sonuc == BOL:
                return None
            print(f"      [429] rate limit, {bekleme} sn bekleniyor "
                  f"(deneme {deneme}/{MAX_THROTTLE_DENEME})...")
            time.sleep(bekleme)
            bekleme *= 2
        return None   # ısrarlı throttle -> son çare olarak bölmeyi dene

    def _tile_isle(self, etiketler: list[tuple[str, str, str]], bbox: tuple, derinlik: int,
                   toplanan: dict[str, tuple], tavan: int,
                   cuisine_haritasi: dict[str, str] | None = None) -> None:
        """Bir tile'ı işler; timeout olursa MAX_DERINLIK'e kadar 4'e böler.
        `tavan` = bu şehir kutusu bitene kadar çıkılabilecek azami toplam boyut."""
        if len(toplanan) >= tavan:
            return   # kutu kotası doldu / hedefe ulaşıldı

        elemanlar = self._istek_dayanikli(
            sorgu_olustur(sorgu_satirlari(etiketler), bbox, self.hedef))

        if elemanlar is None:
            if derinlik >= MAX_DERINLIK:
                print(f"      [!] {derinlik}. derinlikte hâlâ timeout, bu tile atlandı: {bbox}")
                return
            for alt in bbox_dorte_bol(bbox):
                if len(toplanan) >= tavan:
                    return
                self._tile_isle(etiketler, alt, derinlik + 1, toplanan, tavan, cuisine_haritasi)
            return

        for eleman in elemanlar:
            if len(toplanan) >= tavan:
                break
            osm_etiketleri = eleman.get("tags", {})
            isim = osm_etiketleri.get("name")
            if isim and isim not in toplanan and not latin_disi_mi(isim):
                anahtar, deger, ham = ham_etiket_coz(osm_etiketleri, etiketler)
                if self.alt_tip_tavan and self._alt_tip_dolu(toplanan, ham):
                    continue
                # HAM etiket saklanir; alt_tip ondan turetilir (bkz. alt_tip_uygula).
                toplanan[isim] = (int(eleman.get("id") or 0), anahtar, deger, ham)

    def _alt_tip_dolu(self, toplanan: dict[str, tuple], alt_tip: str) -> bool:
        # Tavan HAM alt tipe uygulanir: eritilmis/bekleyen tipler `genel`'e
        # dustugu icin nihai alt tipe bakmak butun kotayi tek kovaya yigardi.
        mevcut = sum(1 for kayit in toplanan.values() if kayit[3] == alt_tip)
        return mevcut >= self.alt_tip_tavan

    def kategori_cek(self, is_kolu: str, etiketler: list[tuple[str, str, str]],
                     grid: list[tuple], adlar: list[str]) -> list[dict]:
        toplanan: dict[str, tuple] = {}   # isim -> (osm_id, anahtar, deger, ham alt tip)
        cuisine_haritasi = IS_KOLU_CUISINE_ALT_TIPLERI.get(is_kolu)
        try:
            for tile, ad in zip(grid, adlar):
                if len(toplanan) >= self.hedef:
                    break
                # kutu_kota=0 -> sınırsız: kutudan çıkan her şeyi al, hedefe kadar devam.
                tavan = (min(self.hedef, len(toplanan) + self.kutu_kota)
                         if self.kutu_kota else self.hedef)
                onceki = len(toplanan)
                self._tile_isle(etiketler, tile, 0, toplanan, tavan, cuisine_haritasi)
                if len(toplanan) > onceki:
                    print(f"      {ad:16} +{len(toplanan) - onceki:4}  (toplam {len(toplanan)})")
        except KeyboardInterrupt:
            # Ctrl+C = "bu kadarı yeter" -> iptal DEĞİL, o ana kadarki isimler
            # kaydedilir ve script durur. Hedefe ulaşmayı beklemeye gerek yok.
            self.kesildi = True
            print(f"\n    [Ctrl+C] {is_kolu} yarıda kesildi, "
                  f"{len(toplanan)} isim KAYDEDİLİYOR.")
        return [{"is_kolu": is_kolu, "isim": isim, "osm_id": oid,
                 "osm_anahtar": anahtar, "osm_deger": deger,
                 "alt_tip": alt_tip_uygula(ham)}
                for isim, (oid, anahtar, deger, ham) in toplanan.items()]

    def etiketleri_cek(self, osm_idler: list[int], paket: int) -> dict[int, dict]:
        """Verilen osm_id'lerin ETIKETLERINI ceker (yeni firma aramaz).
        `node(id:...); out tags;` cok hafif bir sorgu: 18 bin id ~19 istekte biter.
        Donen sozluk: osm_id -> OSM etiketleri. Silinmis node yanitta yer almaz."""
        sonuc: dict[int, dict] = {}
        toplam_paket = (len(osm_idler) + paket - 1) // paket
        for sira in range(toplam_paket):
            dilim = osm_idler[sira * paket:(sira + 1) * paket]
            sorgu = (f"[out:json][timeout:{PER_TILE_TIMEOUT}];\n"
                     f"node(id:{','.join(str(i) for i in dilim)});\nout tags;")
            elemanlar = self._istek_dayanikli(sorgu)
            if elemanlar is None:
                print(f"      [!] paket {sira + 1}/{toplam_paket} alinamadi, atlandi "
                      f"({len(dilim)} id `genel` kalacak)")
                continue
            for eleman in elemanlar:
                sonuc[int(eleman.get("id") or 0)] = eleman.get("tags", {})
            print(f"      paket {sira + 1}/{toplam_paket}  +{len(elemanlar):4}  "
                  f"(toplam {len(sonuc)}/{len(osm_idler)})")
        return sonuc


CSV_KOLONLARI = ["is_kolu", "isim", "osm_id", "osm_anahtar", "osm_deger", "alt_tip"]


def kayitlari_yukle(yol: Path) -> dict[str, list[dict]]:
    """CSV'yi is_kolu -> [satir] olarak okur. Eksik kolonlar (alt_tip'siz ya da
    ham etiketsiz eski dosyalar) bos varsayilanla doldurulur."""
    if not yol.exists():
        return {}
    with open(yol, encoding="utf-8") as f:
        kayitlar = list(csv.DictReader(f))
    gruplar: dict[str, list[dict]] = {}
    for k in kayitlar:
        for kolon in ("osm_anahtar", "osm_deger"):
            k.setdefault(kolon, "")
        k.setdefault("alt_tip", VARSAYILAN_ALT_TIP)
        gruplar.setdefault(k["is_kolu"], []).append(k)
    return gruplar


def mevcut_kayitlari_yukle() -> dict[str, list[dict]]:
    """Daha önce başarıyla çekilmiş kategorileri tekrar çekmemek için (resumable)."""
    return kayitlari_yukle(CIKTI_CSV)


def csv_yaz(gruplar: dict[str, list[dict]], yol: Path | None = None) -> None:
    tum_kayitlar = [k for kayitlar in gruplar.values() for k in kayitlar]
    hedef = yol or CIKTI_CSV
    hedef.parent.mkdir(parents=True, exist_ok=True)
    with open(hedef, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(f, fieldnames=CSV_KOLONLARI, extrasaction="ignore")
        yazici.writeheader()
        for kayit in tum_kayitlar:
            yazici.writerow({kolon: kayit.get(kolon, "") for kolon in CSV_KOLONLARI})


def alt_tip_raporu(gruplar: dict[str, list[dict]]) -> None:
    """HAM etiket dağılımını basar ve her alt tipin akıbetini (aktif / eritildi /
    beklemede) gösterir. Nihai `alt_tip` kolonu yalnız AKTİF tipleri taşır."""
    print("\n[alt tip dağılımı]  (ham OSM etiketi -> akıbet)")
    for is_kolu, etiketler in IS_KOLU_OSM_ETIKETLERI.items():
        kayitlar = gruplar.get(is_kolu) or []
        if not kayitlar:
            continue
        sayac: dict[str, int] = {}
        for k in kayitlar:
            ham = _ETIKET_ALT_TIP.get(
                ((k.get("osm_anahtar") or "").strip(), (k.get("osm_deger") or "").strip()),
                VARSAYILAN_ALT_TIP)
            sayac[ham] = sayac.get(ham, 0) + 1
        bekleniyor = list(dict.fromkeys([t for _, _, t in etiketler]))
        print(f"  {is_kolu:22s} {len(kayitlar):5d} kayıt")
        for t in bekleniyor:
            adet = sayac.get(t, 0)
            nihai = alt_tip_uygula(t)
            if nihai == t:
                akibet = "aktif"
            elif t in ALT_TIP_ERITME:
                akibet = f"eritildi -> {ALT_TIP_ERITME[t]}" + (
                    "" if ALT_TIP_ERITME[t] in AKTIF_ALT_TIPLER else " (beklemede)")
            else:
                akibet = "beklemede (ürün tarafı yok, `genel` yazıldı)"
            uyari = "  [!] taban altı" if 0 < adet < ALT_TIP_TABANI else (
                "  [!] hiç gelmedi" if adet == 0 else "")
            print(f"      {t:18s} {adet:5d}  {akibet}{uyari}")
    tum = [k for kayitlar in gruplar.values() for k in kayitlar]
    aktif = sum(1 for k in tum if (k.get("alt_tip") or VARSAYILAN_ALT_TIP) != VARSAYILAN_ALT_TIP)
    hamsiz = sum(1 for k in tum if not (k.get("osm_anahtar") or "").strip())
    print(f"\n  toplam {len(tum)} kayıt | aktif alt tip {aktif} "
          f"(%{100 * aktif / len(tum):.1f}) | ham etiketi olmayan {hamsiz}")


ETIKET_TAZELE_CIKTI = Path("data/companies/firma_adlari_osm_alt_tip.csv")
VARSAYILAN_ID_PAKETI = 500   # tek sorguda sorulacak osm_id adedi


def etiket_tazele(cekici: "Cekici", kaynak: Path, cikti: Path, paket: int) -> None:
    """Var olan CSV'deki firmaların OSM etiketlerini çeker, ham etiket + alt tip
    kolonlarıyla AYRI bir dosyaya yazar. Yeni firma aramaz, ad değiştirmez:
    registry ile birebir aynı firma kümesi korunur."""
    gruplar = kayitlari_yukle(kaynak)
    if not gruplar:
        raise SystemExit(f"Kaynak CSV okunamadı ya da boş: {kaynak}")

    idli = [k for kayitlar in gruplar.values() for k in kayitlar
            if (k.get("osm_id") or "0").strip() not in ("", "0")]
    osm_idler = sorted({int(k["osm_id"]) for k in idli})
    toplam = sum(len(v) for v in gruplar.values())
    print(f"[+] {kaynak}: {toplam} kayıt, {len(osm_idler)} benzersiz osm_id "
          f"({toplam - len(idli)} kaydın id'si yok, `genel` kalacak)")
    print(f"[+] {paket}'lik paketlerle ~{(len(osm_idler) + paket - 1) // paket} istek")

    etiketler = cekici.etiketleri_cek(osm_idler, paket)

    bulunamayan = 0
    for is_kolu, kayitlar in gruplar.items():
        is_kolu_etiketleri = IS_KOLU_OSM_ETIKETLERI.get(is_kolu, [])
        for kayit in kayitlar:
            osm_etiketleri = etiketler.get(int(kayit.get("osm_id") or 0))
            if not osm_etiketleri:
                bulunamayan += 1
                kayit["osm_anahtar"], kayit["osm_deger"] = "", ""
                kayit["alt_tip"] = VARSAYILAN_ALT_TIP
                continue
            anahtar, deger, ham = ham_etiket_coz(osm_etiketleri, is_kolu_etiketleri)
            kayit["osm_anahtar"], kayit["osm_deger"] = anahtar, deger
            kayit["alt_tip"] = alt_tip_uygula(ham)

    csv_yaz(gruplar, cikti)
    print(f"\n[+] {cikti} yazıldı. OSM'de bulunamayan (silinmiş/değişmiş) {bulunamayan} kayıt.")
    alt_tip_raporu(gruplar)
    print(f"\n[i] {kaynak} ve data/firma_registry.csv DEĞİŞMEDİ. Doğrulama "
          f"temizse birleştirme ayrı adım.")


def yeniden_etiketle(kaynak: Path) -> None:
    """Sözlük değiştiğinde alt_tip'i HAM etiketten yeniden türetir. Ağa çıkmaz."""
    gruplar = kayitlari_yukle(kaynak)
    if not gruplar:
        raise SystemExit(f"CSV okunamadı ya da boş: {kaynak}")
    degisen = 0
    for kayitlar in gruplar.values():
        for kayit in kayitlar:
            yeni = satirdan_alt_tip(kayit)
            if yeni != (kayit.get("alt_tip") or VARSAYILAN_ALT_TIP):
                degisen += 1
            kayit["alt_tip"] = yeni
    csv_yaz(gruplar, kaynak)
    print(f"[+] {kaynak}: {degisen} kaydın alt_tip'i güncellendi (ağ isteği yok).")
    alt_tip_raporu(gruplar)


def main():
    global CIKTI_CSV
    parser = argparse.ArgumentParser(description="OSM tüm-Türkiye firma adı çekici (adaptif bölme)")
    parser.add_argument("--hedef", type=int, default=VARSAYILAN_HEDEF, help="is_kolu başına hedef benzersiz isim")
    parser.add_argument("--bekleme", type=int, default=VARSAYILAN_BEKLEME, help="istekler arası saniye")
    parser.add_argument("--sehir-limit", type=int, default=len(SEHIR_KUTULARI),
                        help="taranacak şehir sayısı (liste büyükten küçüğe sıralı)")
    parser.add_argument("--kutu-kota", type=int, default=0,
                        help="şehir başına azami isim (0 = sınırsız; coğrafi yayılım istersen 100-200 ver)")
    parser.add_argument("--alt-tip-tavan", type=int, default=0,
                        help="alt tip başına azami isim (0 = sınırsız; bol alt tip nadiri ezmesin diye)")
    parser.add_argument("--cikti", type=Path, default=CIKTI_CSV,
                        help="çıktı CSV yolu (deneme koşusunda ver, gerçek dosyanın üstüne yazma)")
    parser.add_argument("--is-kolu", default="",
                        help="yalnız bu iş kollarını çek (virgülle ayrık); boş = hepsi")
    parser.add_argument("--grid-satir", type=int, help=argparse.SUPPRESS)   # eski mod, kullanılmıyor
    parser.add_argument("--grid-sutun", type=int, help=argparse.SUPPRESS)   # eski mod, kullanılmıyor
    parser.add_argument("--sunucu", default="ana", choices=list(OVERPASS_SUNUCULAR),
                        help="Overpass sunucusu; ana tıkanınca 'kumi' veya 'coffee' aynasına geç")
    parser.add_argument("--yeniden", action="store_true",
                        help="mevcut CSV'yi yok say, hepsini baştan çek")
    parser.add_argument("--etiket-tazele", action="store_true",
                        help="yeni firma ARAMA: mevcut CSV'deki osm_id'lerin etiketlerini "
                             "çekip ham etiket + alt_tip kolonlarını AYRI dosyaya yaz")
    parser.add_argument("--kaynak", type=Path, default=CIKTI_CSV,
                        help="--etiket-tazele / --yeniden-etiketle için girdi CSV")
    parser.add_argument("--tazele-cikti", type=Path, default=ETIKET_TAZELE_CIKTI,
                        help="--etiket-tazele çıktısı (kaynağın üstüne YAZMAZ)")
    parser.add_argument("--id-paketi", type=int, default=VARSAYILAN_ID_PAKETI,
                        help="tek Overpass sorgusunda sorulacak osm_id adedi")
    parser.add_argument("--yeniden-etiketle", action="store_true",
                        help="ağa çıkmadan alt_tip'i ham etiketten yeniden türet "
                             "(sözlük değiştiğinde)")
    args = parser.parse_args()

    if args.yeniden_etiketle:
        yeniden_etiketle(args.kaynak)
        return

    CIKTI_CSV = args.cikti

    if args.etiket_tazele:
        sunucu = OVERPASS_SUNUCULAR[args.sunucu]
        print(f"[+] Sunucu: {args.sunucu} ({sunucu})  bekleme={args.bekleme}sn")
        etiket_tazele(Cekici(args.hedef, args.bekleme, 0, sunucu),
                      args.kaynak, args.tazele_cikti, args.id_paketi)
        return

    secili = {s.strip() for s in args.is_kolu.split(",") if s.strip()}
    if secili:
        bilinmeyen = secili - set(IS_KOLU_OSM_ETIKETLERI)
        if bilinmeyen:
            parser.error(f"bilinmeyen is_kolu: {', '.join(sorted(bilinmeyen))}")

    if args.grid_satir or args.grid_sutun:
        print("[!] --grid-satir/--grid-sutun artık kullanılmıyor (şehir kutusu moduna geçildi), "
              "yok sayılıyor.")

    grid = sehir_gridi()[:args.sehir_limit]
    adlar = [s[0] for s in SEHIR_KUTULARI][:args.sehir_limit]
    kutu_kota = args.kutu_kota   # 0 = sınırsız (varsayılan)
    print(f"[+] {len(grid)} şehir kutusu taranacak, hedef={args.hedef}/is_kolu, "
          f"sehir-basi-kota={kutu_kota or 'sinirsiz'}, bekleme={args.bekleme}sn")

    gruplar = {} if args.yeniden else mevcut_kayitlari_yukle()
    if gruplar:
        print(f"[+] Zaten çekilmiş kategoriler (atlanacak): {list(gruplar.keys())}")

    sunucu = OVERPASS_SUNUCULAR[args.sunucu]
    print(f"[+] Sunucu: {args.sunucu} ({sunucu})")
    cekici = Cekici(args.hedef, args.bekleme, kutu_kota, sunucu, args.alt_tip_tavan)

    for is_kolu, etiketler in IS_KOLU_OSM_ETIKETLERI.items():
        if is_kolu in gruplar or (secili and is_kolu not in secili):
            continue
        print(f"[+] {is_kolu} çekiliyor...")
        try:
            kayitlar = cekici.kategori_cek(is_kolu, etiketler, grid, adlar)
        except Exception as e:
            print(f"    [X] HATA: {e} -- bu kategori atlandı, mevcutlar korunuyor.")
            continue
        print(f"    {len(kayitlar)} benzersiz isim ({cekici.istek_sayaci} toplam istek).")
        if kayitlar:
            gruplar[is_kolu] = kayitlar
            csv_yaz(gruplar)   # her kategori sonrası kaydet (kesintiye dayanıklı)
        if cekici.kesildi:
            if kayitlar:
                print(f"    [i] '{is_kolu}' KISMİ olarak kaydedildi. Tekrar çekmek istersen "
                      f"CSV'den bu is_kolu satırlarını sil, yoksa 'tamamlandı' sayılır.")
            else:
                print(f"    [i] '{is_kolu}' için hiç isim toplanmamıştı, CSV'ye yazılmadı; "
                      f"tekrar çalıştırınca baştan denenecek.")
            break

    print(f"\n[+] Toplam {sum(len(v) for v in gruplar.values())} kayıt -> {CIKTI_CSV}")
    alt_tip_raporu(gruplar)


if __name__ == "__main__":
    main()
