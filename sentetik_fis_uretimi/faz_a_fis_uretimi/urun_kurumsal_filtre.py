"""
temiz_urunler.csv KURUMSAL MASRAF filtresi.

NEDEN: havuz ham bir B2C (Trendyol) veri setinden geliyor. Bir masraf fişinde
"Hello Kitty Baskili Pembe Kanvas Ayakkabi", "kres sirt cantasi", "ashwagandha
kapsul", "dondurma alma kasigi" ya da "nevresim takimi" bulunmaz. Faz B'de
olculdu: pilot ciktilarin yarisindan fazlasinda kusur PROMPTTA DEGIL, fisin
kendisindeydi -- model kurumsal gerekce yazamiyor cunku ortada kurumsal bir
harcama yok. Hicbir prompt bunu duzeltemez; kaynaktaki veri hatasidir.

`urun_kategori_duzelt.py` ile ayni kalibin genellestirilmesi: orada yalniz
`yazilim_lisans` icin yapilan temizlik burada bes kategoriye uygulanir.

YONTEM (kategori basina iki katman):
  1. KARA LISTE  -- B2C isaretcisi tasiyan urun elenir (bebek/cocuk, ic giyim,
     ev tekstili/dekorasyon, evcil hayvan, takviye gida, kisisel gadget...).
  2. BEYAZ LISTE -- yalniz `BEYAZ_LISTE_ZORUNLU` kategorilerinde: urun ayrica
     kurumsal bir terime UYMAK ZORUNDA (is kiyafeti, ofis mobilyasi, ofis
     teknolojisi). Bu kategorilerde havuzun neredeyse tamami B2C oldugu icin
     kara liste tek basina yetmiyor.

Kalan kategoriler (temizlik, ofis_sarf_malzeme) dokunulmadan gecer.

IS_KOLU_AGIRLIKLARI havuz uzunluguna baglidir (`1 + log1p(n)`) ama LOGARITMIK
oldugu icin agresif budama dagilimi cokertmez: 8529 -> 800 agirligi ~%23
dusurur. Rapor bu degisimi de basar.

Kullanim:
    python -m faz_a_fis_uretimi.urun_kurumsal_filtre              # RAPOR (dosyaya dokunmaz)
    python -m faz_a_fis_uretimi.urun_kurumsal_filtre --uygula     # yedek alip yazar
    python -m faz_a_fis_uretimi.urun_kurumsal_filtre --ornek 15   # elenen/kalan ornek sayisi

--uygula sonrasi REGENERATE gerekir (main.py), aksi halde diskteki faturalar
eski havuzdan uretilmis kalir.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
from collections import Counter
from pathlib import Path

CSV_YOLU = Path(__file__).parent.parent / "data" / "urun_verileri" / "temiz_urunler.csv"
YEDEK_SON_EK = ".yedek_kurumsal"
YEDEK_ARSIVI = Path("data/backups")


def normalize(metin: str) -> str:
    """Turkce diakritikleri sadelestirip kucuk harfe indirger (desen eslesmesi icin)."""
    esleme = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iissgguuoocc")
    return metin.translate(esleme).lower()


def _desen(kelimeler: list[str], ek_duyarli: bool = False) -> re.Pattern:
    """Kelime-sinirli VEYA deseni. Bastaki `\\b` SART: 'kol' -> 'koltuk'u yakalamasin.

    `ek_duyarli=True` ise SONDAKI `\\b` kaldirilir, yani Turkce ek almis bicimler
    de yakalanir ('bebek' -> 'bebeklere', 'yumusatici' -> 'yumusaticisi').

    NEDEN HER KATEGORIDE ACIK DEGIL (2026-07-30'da olculdu): sondaki `\\b`
    kaldirilinca kisa kelimeler mesru urunleri de eliyor --
      giyim         : 'taki' -> 'Esofman TAKIMI', 'sal' -> 'SALas Gomlek', 'kus' -> 'KUSak'
      kisisel_bakim : 'parfum' -> 'PARFUMsuz Sampuan' (tam TERS anlam!), 'far' -> 'FARmasi' (marka)
    O yuzden kategori bazinda secilir (bkz. KARA_LISTE_EK_DUYARLI): yalniz
    kazanci kaybindan buyuk olan kategorilerde acik.
    """
    son = "" if ek_duyarli else r"\b"
    return re.compile(r"\b(?:" + "|".join(kelimeler) + r")" + son)

KARA_LISTE_EK_DUYARLI = {"temel_gida", "ofis_mobilya"}
# Ek-duyarli (prefix) eslesme YALNIZ bu kategorilerde. Olculen kazanc/kayip:
#   temel_gida   : 190 kacak yakalanir (%10,8), 1 yanlis pozitif ('proteinli tarhana')
#                  -- 'Bebeklere Ozel Camasir Deterjani', 'Bebegimin Ilk Corbasi',
#                  'Cocuklara Ozel Conditioner', 'Babyjem Konak Taragi' bunlar.
#   ofis_mobilya : 2 kacak ('Tv Sehpasi'), yanlis pozitif yok.
# teknoloji_ekipman KAPALI birakildi: 33 kacagin cogu 'kilif' uzerinden
# 'Laptop Cantasi/Kilifi' ve bir sirketin laptop cantasi almasi mesrudur.



# --------------------------------------------------------------------------
# KARA LISTELER -- B2C isaretcileri. Kategori basina ayri tutuluyor cunku ayni
# kelime bir kategoride eleyici, digerinde masum olabilir ('set' her yerde var,
# 'bebek' yalniz gidada/giyimde eleyici, teknolojide zaten yok).
# --------------------------------------------------------------------------

ORTAK_B2C = [
    "bebek", "bebe", "baby", "cocuk", "cocuklu", "kres", "anaokulu", "oyuncak",
    "kedi", "kopek", "evcil", "mama", "kus", "akvaryum", "balik yemi",
    "hello kitty", "disney", "barbie", "spiderman", "frozen",
    # Ic giyim HER kategoride eleyici olmali: 'Seffaf Sutyen Duzenleyici'
    # ofis_mobilya'da beyaz-listedeki 'duzenleyici' teriminden geciyordu
    # (olculdu: 100k'da 377 kalem sizmis).
    "sutyen", "kulot", "boxer", "tanga", "corap", "atlet", "sneaker",
]

KARA_LISTE: dict[str, list[str]] = {
    "giyim": ORTAK_B2C + [
        # ic giyim / plaj / gece
        "kulot", "boxer", "sutyen", "tanga", "hipster", "slip", "bikini", "mayo",
        "pijama", "gecelik", "sabahlik", "atlet", "corap", "patik", "bato",
        "ribana", "dantelli", "fantezi", "jartiyer", "bralet", "bodysuit",
        # gunluk/moda ayakkabi
        "sneaker", "sandalet", "terlik", "babet", "topuklu", "stiletto",
        "kar botu", "postal",
        # kadin moda
        "tayt", "elbise", "etek", "bluz", "tulum elbise", "abiye", "kaftan",
        "sal", "esarp", "bandana", "tesettur", "ferace",
        # aksesuar/taki
        "taki", "kolye", "bileklik", "kupe", "yuzuk", "saat kordonu",
        # spor/hobi
        "yoga", "fitness", "kamp", "trekking", "bisiklet",
    ],
    "ofis_mobilya": ORTAK_B2C + [
        # ev tekstili
        "nevresim", "carsaf", "yastik", "yorgan", "battaniye", "havlu", "bornoz",
        "perde", "hali", "kilim", "pasapas", "ortusu", "runner", "masa ortusu",
        "pike", "yatak ortusu",
        # mutfak / banyo -- GENIS TUTULUYOR: 'duzenleyici/organizer/dolap/cekmece'
        # gibi jenerik beyaz-liste terimleri mutfak urunlerini geciriyordu
        # ('Cekmece Ici Bicaklik', 'Dolap Ici Duzenleyici', 'Baharat Organizer').
        "mutfak", "banyo", "tencere", "tava", "kasik", "kasiklik", "catal",
        "bicak", "bicaklik", "tabak", "bardak", "kupa", "surahi", "sunum",
        "sunumluk", "kesme tahtasi", "dograma", "servis tabagi", "kahvalti",
        "dondurma", "sebzelik", "ekmeklik", "baharat", "baharatlik", "yaglik",
        "sirkelik", "sarimsak", "spatula", "hamur", "saklama", "buz",
        "pecete", "peskir", "cop kovasi", "supurge", "firin", "ocak",
        "klozet", "dus", "lavabo", "sabunluk", "camasir sepeti", "camasir",
        "ayakkabilik", "bambu", "silikon", "kilif", "kilifi", "havluluk",
        "pamuk", "kavanoz", "makyaj", "kulak", "tuvalet kagidi", "sandalye kilifi",
        # dekorasyon / aydinlatma (ev)
        "avize", "sarkit", "abajur", "aplik", "tablo", "biblo", "vazo",
        "mumluk", "cerceve", "dekoratif", "duvar sticker", "saksi",
        "zigon", "sehpa takimi", "puf", "koltuk ortusu",
        # yatak odasi / cocuk odasi
        "yatak odasi", "bas ucu", "gardirop", "sifonyer", "besik", "dolap ici",
        "cekmece ici",
        # 2026-07-30 -- jenerik beyaz terimler cikarildiktan SONRA hala gecen
        # baglamlar. Hepsi DAR ve iki-kelimeli tutuldu; tek kelimelik genis
        # bir terim ('stand', 'sepet') mesru ofis urunlerini de olurdu.
        "buzdolabi", "bavul", "valiz", "baza alti", "petek ustu", "tezgah ustu",
        "lambader", "takilik", "taki standi", "kupelik", "kolye", "bileklik",
        "mouse pad", "gaming", "oyuncu", "gamer",
        "balkon", "bahce", "teras", "rattan", "kanepe", "oturma grubu",
        "tv sehpa", "tv unitesi", "tepsi", "tepsisi", "tesbih",
        "folyo", "folyosu", "kaplama", "duvar rafi", "ucan raf", "gizli raf",
        # EK-DUYARLI VARYANTLAR: _desen `\b(?:...)\b` kurdugu icin (satir 58)
        # kara liste Turkce ekli bicimleri KACIRIYOR -- 'cerceve' deseni
        # 'Cercevesi'ni yakalamiyordu ve 'Evrak Cercevesi' havuza girmisti.
        # _desen'i topluca gevsetmek bes kategoride asiri-eslesme riski
        # tasidigi icin ekli bicimler tek tek yaziliyor.
        "cercevesi", "cerceveli", "ortusunu", "yastigi", "mutfagi",
        "battaniyesi", "salı", "sali", "saati", "hali yikama",
        # Jenerik "ofis" beyaz terimi (cikarilamaz, en onemli terim) dekorasyon ve
        # ev aydinlatmasini geciriyordu: 'Tavan Wc Kiler OFIS Lambasi', 'Roma
        # Rakamli Saat ... Dua Et' (duvar susu). Beyaz terimi zayiflatmak yerine
        # bu baglamlar kara listeye alindi.
        "roma rakamli", "dua", "allah", "muhammed", "vav",
        "wc", "kiler", "tavan", "armatur", "sıva ustu", "siva ustu",
        # Imla/bitisik yazim kacaklari (ayni \b katiligi): kara listede 'gardirop'
        # var ama urun 'Gardrop' yazmis; 'zigon' var ama 'Zigonsehpa' bitisik
        # oldugu icin sondaki \b tutmuyor.
        "gardrop", "zigonsehpa",
        # Jenerik 'dolap' ve 'koltuk' beyaz terimlerinin gecirdikleri (bunlar
        # cikarilamaz -- 'dosya dolabi' ve 'ofis koltugu' kategorinin cekirdegi).
        # UZUN KUYRUK: burada durduruldu, kalan sizinti ~%6. Kuratorlu cekirdek
        # (40 kalem, havuzun %29'u) gercekci bir taban garanti ediyor.
        "kiyafet", "t shirt", "tshirt", "armut koltuk", "bez dolap",
        "dolap organizeri", "yatak dolap",
    ],
    "temel_gida": ORTAK_B2C + [
        # takviye / sporcu gidasi
        "protein", "whey", "kreatin", "bcaa", "glutamin", "pre-workout",
        "preworkout", "kapsul", "tablet", "takviye", "vitamin", "mineral",
        "kollajen", "kolajen", "ashwagandha", "gaba", "omega", "probiyotik",
        "l-karnitin", "karnitin", "melatonin", "magnezyum", "cinko",
        "nutrition", "sports", "supplement", "bigjoy", "gainer",
        # bebek bakim (gida disi ama kategoriye sizmis). MARKA ADLARI SART:
        # 'Otribebe', 'Aptamil', 'Mustela' gibi adlar 'bebek' kelimesi
        # gecmediginden kelime-sinirli desenden kaciyordu.
        "emzik", "biberon", "muslin", "islak mendil", "pisik", "alistirma",
        "gogus pedi", "gogus ucu", "catlak kremi", "mama sandalyesi", "bez",
        "yenidogan", "emzirme", "aspirator", "burun aspiratoru", "dis granulu",
        "otribebe", "weebaby", "brunobaby", "aptamil", "bebelac", "milupa",
        "hipp", "mustela", "lansinoh", "chicco", "philips avent", "nuk",
        "sebamed", "uni baby", "prima", "molfix", "huggies", "pampers",
        # ev/kisisel bakim sizintisi
        "macunu", "fircasi", "tuy", "silikon", "deterjan", "yumusatici",
        # gida DISI sizintilar (marka adi kelime-sinirini deldigi icin acikca yazildi)
        "bebekevi", "marimo", "ates olcer", "atesolcer", "termometre",
        "klavye", "airfryer", "fritoz", "pisirme kagidi", "melamin",
        # KOZMETIK, gida kategorisine YANLIS etiketlenmis (kaynak veri hatasi):
        # 'Gogus Bakim Kremi', 'Gogus Dolgunlastirici'. Desenler DAR yazildi --
        # yalin 'gogus' kullanilamaz, 'Gogus ve Sirt Cantasi' mesru bir urun.
        "gogus bakim", "gogus kremi", "gogus dolgunlastirici", "gogus kalkani",
        "dolgunlastirici", "dikleştirici", "diklestirici", "sarkiklik",
        "anne sutu", "gida takviyesi", "sut arttirici",
    ],
    "kisisel_bakim": ORTAK_B2C + [
        # TEDAVI/BAKIM kozmetigi -- kurumsal masrafta karsiligi yok. Hijyen
        # sarfi (sabun, dezenfektan, tiras) beyaz listede KALIR.
        "leke karsiti", "leke karşıtı", "anti aging", "anti-aging", "yaslanma",
        "yaşlanma", "kirisiklik", "kırışıklık", "gozenek", "gözenek",
        "dokulme karsiti", "dökülme karşıtı", "biotin", "kolajen", "collagen",
        "aydinlatici", "aydınlatıcı", "sivilce", "sivilce karsiti",
        # hobi kozmetik / parfumeri (kurumsal masrafta karsiligi yok)
        "parfum", "edp", "edt", "kolonya sise", "makyaj", "ruj", "far", "maskara",
        "oje", "manikur", "pedikur", "kirpik", "kas", "peeling", "serum",
        "keratin", "sac boyasi", "boya", "roze", "botoks", "dolgu",
        "ucucu yag", "aroma terapi", "aromaterapi", "masaj yagi",
        "cinsel", "prezervatif", "kayganlastirici", "epilasyon", "agda",
    ],
    "teknoloji_ekipman": ORTAK_B2C + [
        # kisisel gadget / giyilebilir
        "watch", "kordon", "akilli saat", "bileklik", "airpods", "kulaklik kilifi",
        "telefon kilifi", "kilif", "ekran koruyucu", "temperli",
        # beyaz esya / kisisel bakim cihazi
        "sac kurutma", "sac duzlestirici", "sac maasi", "epilasyon",
        "tiras makinesi", "dis fircasi", "supurge", "utu", "blender",
        "kettle", "su isitici", "tost makinesi", "airfryer", "fritoz",
        "dikis makinesi", "mikser", "mutfak robotu",
        # eglence
        "gaming", "oyuncu", "konsol", "playstation", "xbox", "joystick",
        "uydu alici", "uydu", "anten", "televizyon", "tv",
        # icerik uretici / sosyal medya ekipmani -- beyaz-listedeki 'kamera',
        # 'telefon', 'usb' terimlerinden geciyorlardi (olculdu: 1095 kalem).
        # Pilotta bir ofis fisinde 'gizli kamera + influencer makyaj isigi +
        # ring light' yan yana cikti.
        "ring light", "ringlight", "youtuber", "tiktok", "influencer",
        "vlog", "selfie", "tripod", "tripot", "sipsak", "polaroid",
        "gizli kamera", "dinleme", "makyaj isigi", "cekim isigi",
    ],
}

# --------------------------------------------------------------------------
# BEYAZ LISTELER -- yalniz asagidaki kategorilerde ZORUNLU. Bu uc havuzun
# neredeyse tamami B2C oldugu icin "elemeye calismak" yerine "kabul edileni
# saymak" daha guvenli: kara liste tek basina birakilirsa 'Pembe Potin Kadin
# Sneaker' gibi ureyen sonsuz varyasyon sizmaya devam eder.
# --------------------------------------------------------------------------

BEYAZ_LISTE: dict[str, list[str]] = {
    "giyim": [
        # is kiyafeti / koruyucu ekipman
        "is elbisesi", "is kiyafeti", "is pantolonu", "is gomlegi", "tulum",
        "onluk", "forma", "uniforma", "yelek", "reflektorlu", "reflektif",
        "baret", "kask", "is eldiveni", "eldiven", "is ayakkabisi",
        "guvenlik ayakkabisi", "celik burun", "is botu", "yagmurluk",
        "mont", "parka", "polar", "sweatshirt", "kaban",
        # kurumsal kiyafet
        "gomlek", "pantolon", "takim elbise", "ceket", "blazer", "kravat",
        "papyon", "kemer", "klasik ayakkabi", "deri ayakkabi", "oxford",
        # is cantasi
        "evrak cantasi", "laptop cantasi", "notebook cantasi", "bilgisayar cantasi",
        "valiz", "seyahat cantasi", "sirt cantasi",
    ],
    # NOT: jenerik terimler (sunum, kasa, kilit, ayakli, bolme, cekmece) BILEREK
    # CIKARILDI -- mutfak/ev urunlerini geciriyorlardi. 'duzenleyici/organizer/
    # dolap/raf' kaldi ama kara listedeki mutfak sozcukleri onlari dengeliyor.
    "ofis_mobilya": [
        "ofis", "calisma masasi", "toplanti masasi", "buro", "sandalye",
        "koltuk", "sehpa", "dolap", "dosya dolabi", "kitaplik",
        "etajer", "keson", "pano", "beyaz tahta", "yazi tahtasi",
        "portmanto", "askilik", "projeksiyon perdesi",
        "evrak", "dosyalik", "masa lambasi",
        "monitor standi", "laptop standi",
        # 2026-07-30: JENERIK UCLU CIKARILDI -> "raf", "duzenleyici", "organizer".
        # Olculdu: 249 kalemlik havuzun %25'ini tek basina "organizer", %17'sini
        # "raf" geciriyordu ve gecirdikleri ofis urunu DEGILDI: 'Buzdolabi Ici
        # Organizer', 'Bavul Ici Duzenleyici', 'Baza Alti Organizer', 'Sise
        # Duzenleyici Raf', 'Ucan Raf', 'Petek Ustu Raf'. Bu, §17.1'in
        # "kalibrasyon tuzagi" notunun `sunum`/`kasa`/`cekmece` icin soyledigi
        # durumun aynisi -- jenerik terim mutfak/ev urununu geciriyor.
        # Cozum kara listeye eklemek DEGIL (kara liste once calisir, "monitor
        # standi" gibi mesru beyaz terimleri de oldururdu); jenerik terimi
        # cikarip DAR bicimlerini yazmak.
        "dosya rafi", "evrak rafi", "arsiv rafi", "masa ustu duzenleyici",
        "kalemlik", "flipchart", "su sebili",
    ],
    # kisisel_bakim havuzu da (giyim gibi) neredeyse tamamen B2C kozmetik.
    # Kurumsal karsiligi olan tek alt kume HIJYEN/SARF: el sabunu, dezenfektan,
    # kagit havlu, tiras, dis bakimi. Bakim/tedavi kozmetigi (losyon, serum,
    # maske, anti-aging) beyaz listeye ALINMADI.
    "kisisel_bakim": [
        "sabun", "sivi sabun", "el sabunu", "kofre", "dezenfektan",
        "antibakteriyel", "hijyen", "kolonya", "islak mendil", "mendil",
        "kagit havlu", "tuvalet kagidi", "pecete", "dus jeli", "sampuan",
        "sac kremi", "tiras", "jilet", "kopuk", "dis macunu", "dis fircasi",
        "agiz bakim", "gargara", "deodorant", "roll on", "el kremi",
        "gunes kremi", "eldiven", "maske", "bone", "galos",
    ],
    "teknoloji_ekipman": [
        "laptop", "notebook", "bilgisayar", "masaustu", "monitor", "ekran",
        "yazici", "printer", "tarayici", "scanner", "toner", "kartus",
        "klavye", "mouse", "fare", "docking", "usb", "hub", "adaptor",
        "ssd", "harddisk", "hdd", "harici disk", "bellek", "ram",
        "projeksiyon", "projektor", "barkod", "yazarkasa", "pos",
        "kamera", "webcam", "mikrofon", "kulaklik", "hoparlor",
        "switch", "router", "modem", "access point", "kablo", "sunucu",
        "server", "nas", "ups", "kesintisiz guc", "telefon", "tablet",
        "kahve makinesi", "su sebili",
    ],
}

BEYAZ_LISTE_ZORUNLU = set(BEYAZ_LISTE.keys())

_KARA_DESEN = {
    kat: _desen([normalize(k) for k in kelimeler], kat in KARA_LISTE_EK_DUYARLI)
    for kat, kelimeler in KARA_LISTE.items()
}
_BEYAZ_DESEN = {kat: _desen([normalize(k) for k in kelimeler]) for kat, kelimeler in BEYAZ_LISTE.items()}


def urun_kurumsal_mi(baslik: str, kategori: str) -> tuple[bool, str]:
    """(kalsin_mi, gerekce) -- gerekce raporda gruplanir."""
    metin = normalize(baslik)

    kara = _KARA_DESEN.get(kategori)
    if kara:
        eslesme = kara.search(metin)
        if eslesme:
            return False, f"kara_liste:{eslesme.group(0)}"

    if kategori in BEYAZ_LISTE_ZORUNLU:
        if not _BEYAZ_DESEN[kategori].search(metin):
            return False, "beyaz_liste_disi"

    return True, "kalir"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", default=str(CSV_YOLU), help="temiz_urunler.csv yolu")
    parser.add_argument("--uygula", action="store_true", help="Yedek alip dosyayi YENIDEN YAZAR")
    parser.add_argument("--ornek", type=int, default=8, help="Kategori basina gosterilecek ornek sayisi")
    args = parser.parse_args()

    yol = Path(args.csv)
    if not yol.exists():
        raise SystemExit(f"CSV bulunamadi: {yol}")

    with open(yol, encoding="utf-8-sig") as f:
        satirlar = list(csv.DictReader(f))

    kalanlar: list[dict] = []
    elenen_ornek: dict[str, list[str]] = {}
    kalan_ornek: dict[str, list[str]] = {}
    gerekce_sayaci: dict[str, Counter] = {}
    once = Counter(s["harcama_kategorisi"] for s in satirlar)

    for satir in satirlar:
        kategori = satir["harcama_kategorisi"]
        kalsin, gerekce = urun_kurumsal_mi(satir["title"], kategori)
        if kalsin:
            kalanlar.append(satir)
            kalan_ornek.setdefault(kategori, []).append(satir["title"])
        else:
            elenen_ornek.setdefault(kategori, []).append(satir["title"])
            gerekce_sayaci.setdefault(kategori, Counter())[gerekce.split(":")[0]] += 1

    sonra = Counter(s["harcama_kategorisi"] for s in kalanlar)

    import math
    print("=" * 74)
    print("KURUMSAL MASRAF FILTRESI" + ("  [UYGULANIYOR]" if args.uygula else "  [RAPOR MODU]"))
    print("=" * 74)
    print(f"\n{'kategori':22s} {'once':>6s} {'sonra':>6s} {'elenen':>7s} {'agirlik':>16s}")
    for kategori in sorted(once, key=lambda k: -once[k]):
        o, s = once[kategori], sonra.get(kategori, 0)
        a_once, a_sonra = 1 + math.log1p(o), 1 + math.log1p(s)
        fark = f"{a_once:.2f} -> {a_sonra:.2f}"
        print(f"{kategori:22s} {o:6d} {s:6d} {o - s:7d} {fark:>16s}")
    print(f"{'TOPLAM':22s} {len(satirlar):6d} {len(kalanlar):6d} {len(satirlar) - len(kalanlar):7d}")

    for kategori in sorted(elenen_ornek, key=lambda k: -len(elenen_ornek[k])):
        print(f"\n--- {kategori}: elenme gerekceleri {dict(gerekce_sayaci[kategori])}")
        for baslik in elenen_ornek[kategori][: args.ornek]:
            print(f"    ELENDI  {baslik[:74]}")
        for baslik in kalan_ornek.get(kategori, [])[: args.ornek]:
            print(f"    kalir   {baslik[:74]}")

    bos = [k for k in once if sonra.get(k, 0) == 0]
    if bos:
        print(f"\n!! UYARI: su kategoriler TAMAMEN bosaldi: {bos}")
    kucuk = [k for k in once if 0 < sonra.get(k, 0) < 50]
    if kucuk:
        print(f"\n!! DIKKAT: 50'nin altina dusen kategoriler: {[(k, sonra[k]) for k in kucuk]}")

    if not args.uygula:
        print("\n[i] RAPOR MODU -- dosyaya dokunulmadi. Yazmak icin --uygula ekleyin.")
        return

    # Yedek YALNIZ ilk calistirmada alinir. Script ikinci kez calistirilirsa
    # (ör. desen listesi guncellenip yeniden uygulanirsa) girdi ZATEN
    # filtrelenmis olur; korumasiz bir copy2 orijinal yedegi filtrelenmis
    # surumle ezip geri donusu imkansiz kilardi.
    # Yedek data/backups/ altina tasinmis olabilir; iki yere de bak, yoksa
    # ikinci kosu FILTRELENMIS dosyayi "yedek" diye kaydeder.
    yedek = yol.with_suffix(yol.suffix + YEDEK_SON_EK)
    arsiv = YEDEK_ARSIVI / yol.parent.name / yedek.name
    if yedek.exists() or arsiv.exists():
        print(f"\n[i] Yedek zaten var, KORUNUYOR (uzerine yazilmadi): "
              f"{yedek if yedek.exists() else arsiv}")
    else:
        shutil.copy2(yol, yedek)
    with open(yol, "w", encoding="utf-8", newline="") as f:
        yazici = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
        yazici.writeheader()
        yazici.writerows(kalanlar)

    print(f"[+] Yazildi -> {yol}  ({len(kalanlar)} satir)")
    print("[!] REGENERATE gerekir: python -m faz_a_fis_uretimi.main --count 100000 --anomali-orani 0.25 ...")


if __name__ == "__main__":
    main()
