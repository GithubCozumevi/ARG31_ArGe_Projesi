"""
restoran_urunleri.csv'ye `alt_tipler` kolonu ekler ve `ev_yemekleri` bölümünü
doğurur (2026-08-17 taksonomi turu). `kategori` kolonu (fiş/menü GÖRÜNTÜLEME
bölümü) DEĞİŞMEDEN kalır; `alt_tipler` ayrı bir eksen -- "bu kalemi hangi dar
tip satabilir" (market_urunleri_ozet.csv ile AYNI desen, ';' ile çoklu üyelik).

Market'ten farklı olarak burada REGEX kullanılmıyor: restoran havuzu küçük
(302 kalem) ve el ile küratörlü, o yüzden anahtar kelime taramasının ürettiği
riski (bkz. market_alt_tip_etiketle.py'nin kuruyemişçi/aroma tuzağı) baştan
elemek için doğrudan KÜRATÖRLÜ eşleme kullanılıyor.

Taksonomi kararları (konuşma geçmişi):
  - `doner_kebap` bölümü TEK bölüm olarak kalır (görüntüde) ama kalem başına
    dört ayrı dar tipe böl��nür: donerci / tantunici / kebapci / cigerci.
    İskender kebapçıyla ortak (ayrı `iskenderci` YOK, havuzu tek kalemdi).
  - `kokorec` bölümündeki "Ciğer Dürüm" cigerci'ye taşınır (aynı kimlik,
    doner_kebap'taki Ciğer Şiş ile birleşir). Midye kalemleri hem kokoreçci
    hem balıkçıda satılabilir -- gerçek hayatta ikisi de midye satar,
    tek sahiplik ZORLAMASI aşırı sert olurdu.
  - `ana_yemekler_et/tavuk`, `ara_sicaklar_mezeler`, `icecekler`,
    `hizmet_ve_ekstralar`, YENİ `ev_yemekleri` -- alt_tipler BOŞ. Bunlar dar
    kimlik taşımıyor, yalnız `genel`e (tam menü lokanta) ait.
  - `corbalar` İSTİSNA (2026-08-17): saf çorbacı/işkembeci de gerçek bir
    esnaf kimliği (Aşiyan İşkembe gibi -- sırf çorba satar, kebap/pide
    satmaz), o yüzden `corbaci` diye NORMAL bir dar tip aldı (diğerleriyle
    aynı seviyede). Bölümdeki eksik olan İşkembe/Kelle Paça Çorbası eklendi.
    AMA çorba gerçek hayatta hemen her lokantada YAN ürün olarak da bulunur
    -- bu EVRENSEL erişim `alt_tipler` kolonuyla değil, field_generator
    kablolanırken KATEGORİ bazlı bir liste ile sağlanacak (bkz. "Yürürlükteki
    kararlar" -- EVRENSEL_KATEGORILER kavramı, alt_tipler'ın boş/dolu
    olmasından BAĞIMSIZ): `corbaci` firma tipi yalnız bu bölüme kilitliyken,
    diğer TÜM firma tipleri (dar ya da genel) `corbalar` kategorisine
    otomatik erişecek. Aynı mantık `icecekler`/`ara_sicaklar_mezeler`/
    `hizmet_ve_ekstralar` için de geçerli olacak, onlar için ayrı dar tip
    açılmadı çünkü tek başına bir dükkan kimliği değiller (kimse yalnız
    "içecekçi" ya da "meze salonu" değildir Türkiye bağlamında -- çorbacı
    farklı, gerçekten var).
  - `uzakdogu` (sushi+Asya) BÖLÜNMEDİ -- kullanıcı kararı.
  - `pide_lahmacun` gözleme dahil BÖLÜNMEDİ -- kullanıcı kararı.
  - `pastane_tatli`/`tatlilar` arasındaki kalem tekrarı (Poğaça, Su Böreği,
    Cheesecake ailesi) SORUN DEĞİL: iki ayrı satır, iki ayrı menü bölümü,
    her biri kendi bölümünün etiketini alır -- gerçek hayatta hem börekçi
    hem pastane poğaça satabilir, tek satıra indirgemeye gerek yok.

Kullanım:
    python -m faz_a_fis_uretimi.restoran_alt_tip_etiketle            # CSV'yi günceller
    python -m faz_a_fis_uretimi.restoran_alt_tip_etiketle --dry-run   # yalnız rapor
"""

import argparse
import csv
from pathlib import Path

CSV_YOLU = Path("data/urun_verileri/restoran_urunleri.csv")

# Bölüm -> varsayılan dar alt tip. None = dar kimlik yok (evrensel ya da
# yalnız `genel`e ait, bkz. modül docstring'i).
BOLUM_VARSAYILAN: dict[str, str | None] = {
    "ana_yemekler_et": None,
    "ana_yemekler_tavuk": None,
    "doner_kebap": None,          # kalem bazlı, asagida
    "pide_lahmacun": "pideci_lahmacunci",
    "pizza": "pizzaci",
    "burger": "burgerci",
    "tost_sandvic": "tostcu",
    "borek": "borekci",
    "kokorec": "kokorecci",       # kalem bazli istisna asagida
    "cigkofte": "cigkofteci",
    "balik": "balikci",           # kalem bazli istisna asagida
    "uzakdogu": "uzakdogu",
    "corbalar": "corbaci",        # 2026-08-17: saf corbaci/iskembeci de var (Asiyan Iskembe ornegi)
    "ara_sicaklar_mezeler": None,
    "tatlilar": "tatlici",
    "pastane_tatli": "pastane",
    "icecekler": None,
    "hizmet_ve_ekstralar": None,
    "ev_yemekleri": None,
    "tavukcu": "tavukcu",
}

# YENİ bölüm: tavukçu (kanat/but salonu -- ayrı esnaf kimliği, 2026-08-17
# kullanıcı talebi). Diğer dar tiplerle AYNI seviyede, ozel bir tasima/istisna
# mantığı YOK -- ev_yemekleri ile birebir aynı desen (bkz. main()). Mevcut
# ana_yemekler_tavuk satırlarına DOKUNULMADI, o bölüm (Pirzola/Bonfile/Güveç/
# Barbekü Soslu vb.) hâlâ yalnız `genel`e ait, tam lokanta yemeği.
TAVUKCU: list[str] = [
    "Tavuk Kanat", "Acılı Kanat", "Bal Hardal Soslu Kanat", "Tavuk But",
    "Baharatlı But", "Çıtır But", "Tavuk Şiş", "Yarım Tavuk Izgara",
    "Bütün Tavuk Izgara", "Piliç Izgara", "Köy Tavuğu Izgara",
    "Kanat Tabağı (Karışık)",
]

# doner_kebap bölümü kalem bazlı bölünüyor (bkz. docstring).
DONER_KEBAP_KALEM: dict[str, str] = {
    "Döner Sandviç": "donerci",
    "Lavaş Tavuk": "donerci",
    "Lavaş Et": "donerci",
    "Tantuni": "tantunici",
    "Tavuk Tantuni": "tantunici",
    "Et Tantuni": "tantunici",
    "Et Döner Porsiyon": "donerci",
    "Tavuk Döner Porsiyon": "donerci",
    "Dürüm Döner": "donerci",
    "Porsiyon İskender": "kebapci",
    "Adana Kebap": "kebapci",
    "Urfa Kebap": "kebapci",
    "Beyti Kebap": "kebapci",
    "Ciğer Şiş": "cigerci",
    "Kuzu Şiş": "kebapci",
    "Patlıcan Kebabı": "kebapci",
}

# kokorec/balik icindeki istisnalar (bolum varsayilanini EZER).
KALEM_ISTISNA: dict[str, str] = {
    "Ciğer Dürüm": "cigerci",              # kokorec bolumunde ama ciger kimligi
    "Midye Dolma (10 Adet)": "kokorecci;balikci",
    "Midye Tava Porsiyon": "kokorecci;balikci",
    "Midye Dolma": "kokorecci;balikci",    # balik bolumundeki ayni isim
    "Midye Tava": "kokorecci;balikci",
}

# YENİ bölüm: ev yemekleri. `genel`e çeşitlilik katmak için (2026-08-17
# kararı) -- ne özel bir esnaf kimliği ne de OSM'den gelebilecek bir sinyal,
# doğrudan `genel` firmaların menüsüne ek çeşitlilik.
EV_YEMEKLERI: list[str] = [
    "Kuru Fasulye", "Etli Kuru Fasulye", "Nohut Yemeği", "Zeytinyağlı Dolma",
    "Etli Yaprak Sarma", "Karnıyarık", "İmam Bayıldı", "Patlıcan Musakka",
    "Etli Bamya", "Türlü", "Nohutlu Pirinç Pilavı", "Bulgur Pilavı",
]

# corbalar bölümünde eksikti -- tam da saf çorbacı/işkembeci kimliğinin asıl
# ürünü (Aşiyan İşkembe örneği).
CORBALAR_EKSIK: list[str] = ["İşkembe Çorbası", "Kelle Paça Çorbası", "Beyran Çorbası"]

# tost_sandvic bölümünde eksikti -- büfe kökenli iki kalem (2026-08-17,
# kullanıcı talebi): Patso ekmek arası patates, Patsosis altında sosis de olan
# hâli. Ayrı bir `bufeci` dar tip AÇILMADI -- kullanıcı kararı, tostcu yeterli.
TOSTCU_EKSIK: list[str] = ["Patso", "Patsosis"]


def alt_tip_hesapla(kategori: str, urun_adi: str) -> str:
    if urun_adi in KALEM_ISTISNA:
        return KALEM_ISTISNA[urun_adi]
    if kategori == "doner_kebap":
        return DONER_KEBAP_KALEM.get(urun_adi, "")
    return BOLUM_VARSAYILAN.get(kategori) or ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    satirlar = list(csv.DictReader(open(CSV_YOLU, encoding="utf-8")))
    # (kategori, urun_adi) ile essiz -- ayni isim FARKLI bolumde tekrar
    # edebilir (Pogaca hem borek hem pastane_tatli'de oldugu gibi, bkz.
    # docstring), o yuzden dedup TEK BASINA urun_adi'ya bakamaz.
    mevcut = {(r["kategori"], r["urun_adi"]) for r in satirlar}

    for ad in EV_YEMEKLERI:
        if ("ev_yemekleri", ad) not in mevcut:
            satirlar.append({"kategori": "ev_yemekleri", "urun_adi": ad})

    for ad in TAVUKCU:
        if ("tavukcu", ad) not in mevcut:
            satirlar.append({"kategori": "tavukcu", "urun_adi": ad})

    for ad in CORBALAR_EKSIK:
        if ("corbalar", ad) not in mevcut:
            satirlar.append({"kategori": "corbalar", "urun_adi": ad})

    for ad in TOSTCU_EKSIK:
        if ("tost_sandvic", ad) not in mevcut:
            satirlar.append({"kategori": "tost_sandvic", "urun_adi": ad})

    kategori_sayim: dict[str, int] = {}
    for r in satirlar:
        etiket = alt_tip_hesapla(r["kategori"], r["urun_adi"])
        r["alt_tipler"] = etiket
        if etiket:
            for e in etiket.split(";"):
                kategori_sayim[e] = kategori_sayim.get(e, 0) + 1
        if r["kategori"] == "doner_kebap" and not etiket:
            print(f"[!] doner_kebap kalemi eşlenmedi: {r['urun_adi']!r} -- DONER_KEBAP_KALEM'e ekle")

    print(f"[+] Toplam {len(satirlar)} satır ({len(EV_YEMEKLERI)} ev_yemekleri + "
          f"{len(TAVUKCU)} tavukcu yeni kalem dahil).")
    for etiket, n in sorted(kategori_sayim.items(), key=lambda x: -x[1]):
        print(f"    {etiket:<20} {n}")
    bos = sum(1 for r in satirlar if not r["alt_tipler"])
    print(f"    {'(bos -- genel/evrensel)':<20} {bos}")

    if args.dry_run:
        print("[--dry-run] CSV yazılmadı.")
        return

    with open(CSV_YOLU, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(f, fieldnames=["kategori", "urun_adi", "alt_tipler"])
        yazici.writeheader()
        yazici.writerows(satirlar)
    print(f"[+] {CSV_YOLU} güncellendi.")


if __name__ == "__main__":
    main()
