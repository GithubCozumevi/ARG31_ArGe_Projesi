"""
`data/firma_registry.csv`'deki HER firma için sentetik ama gerçekçi bir Türkçe
adres üretir; `data/firma_registry_adres.csv`'ye (registry'nin birebir kopyası
+ yeni `adres` kolonu) yazar.

Format tasarımı 11 GERÇEK fiş adresinden türetildi (kullanıcı 2026-08-26'da
paylaştı) -- tek bir sabit kalıp yerine, gözlenen eksenlerde olasılık ağırlıklı
bir karışım kurulur (bkz. modül içindeki sabitler ve yorumlar):
  - il/ilçe: İL_ILCE_HAVUZU'ndan GERÇEK çiftler (rastgele kombinasyon değil)
  - yapı: mahalle+cadde/sokak+no (%55) / mahallesiz cadde+no (%25) /
    minimal yalnız ilçe (%10) / mevkii-AVM-yol tipi (%10)
  - kısaltma stili (MAH./MH./MH/Mahallesi vb.) fiş içinde bile tutarsız --
    bağımsız rastgele seçilir
  - bitiş biçimi (İLÇE/ŞEHİR, İLÇE/ ŞEHİR, İLÇE ŞEHİR, yalnız İLÇE) karışık
  - büyük harf ~%90, diyakritiksiz (ASCII) yazım ~%10-15, posta kodu ~%10-15
    (posta kodu oranı ÖLÇÜLMEDİ -- 11 örneğin 0'ında vardı, düşük tutuldu)

Anahtar `satici_unvan` (firma adı), `satici_vkn`/`satici_kimlik` DEĞİL:
`generators/anomaly_injector.py:gecersiz_kimlik_no_anomali_uret` yalnız VKN'nin
checksum hanesini bozuyor, unvana hiç dokunmuyor -- registry'de unvan %100
benzersiz (VKN de öyle ama anomaliyle kasıtlı bozulabilen tek alan o).

Kullanım:
    python -m faz_a_fis_uretimi.firma_registry_adres_uret
"""

import csv
import random
import unicodedata
from pathlib import Path

KAYNAK = Path("data/firma_registry.csv")
HEDEF = Path("data/firma_registry_adres.csv")
SEED = 42

# ---------------------------------------------------------------------------
# İl/ilçe havuzu -- GERÇEK çiftler. Büyükşehirler daha çok ilçeyle temsil
# edilerek doğal olarak ağırlıklı seçiliyor (random.choice düz listeden).
# 11 örnekteki çiftler (Sarıyer/İstanbul, Kağıthane/İstanbul, Avcılar/İstanbul,
# Kadıköy/İstanbul, Üsküdar/İstanbul, Odunpazarı/Eskişehir, Tepebaşı/Eskişehir,
# Merkez/Bolu, Göreme/Nevşehir) dahil.
# ---------------------------------------------------------------------------
IL_ILCE_HAVUZU: list[tuple[str, str]] = [
    # İstanbul (büyükşehir, en ağırlıklı)
    ("Sarıyer", "İstanbul"), ("Kağıthane", "İstanbul"), ("Avcılar", "İstanbul"),
    ("Kadıköy", "İstanbul"), ("Üsküdar", "İstanbul"), ("Maltepe", "İstanbul"),
    ("Beşiktaş", "İstanbul"), ("Şişli", "İstanbul"), ("Bakırköy", "İstanbul"),
    ("Kartal", "İstanbul"), ("Pendik", "İstanbul"), ("Ümraniye", "İstanbul"),
    ("Beylikdüzü", "İstanbul"), ("Bahçelievler", "İstanbul"), ("Zeytinburnu", "İstanbul"),
    ("Fatih", "İstanbul"), ("Beyoğlu", "İstanbul"), ("Ataşehir", "İstanbul"),
    ("Sancaktepe", "İstanbul"), ("Esenyurt", "İstanbul"), ("Büyükçekmece", "İstanbul"),
    ("Çekmeköy", "İstanbul"), ("Sultanbeyli", "İstanbul"), ("Tuzla", "İstanbul"),
    ("Başakşehir", "İstanbul"), ("Eyüpsultan", "İstanbul"), ("Bağcılar", "İstanbul"),
    ("Bayrampaşa", "İstanbul"), ("Güngören", "İstanbul"), ("Küçükçekmece", "İstanbul"),
    ("Sultangazi", "İstanbul"), ("Arnavutköy", "İstanbul"), ("Silivri", "İstanbul"),
    # Ankara
    ("Çankaya", "Ankara"), ("Keçiören", "Ankara"), ("Yenimahalle", "Ankara"),
    ("Mamak", "Ankara"), ("Etimesgut", "Ankara"), ("Sincan", "Ankara"),
    ("Altındağ", "Ankara"), ("Gölbaşı", "Ankara"), ("Polatlı", "Ankara"),
    # İzmir
    ("Konak", "İzmir"), ("Bornova", "İzmir"), ("Karşıyaka", "İzmir"),
    ("Buca", "İzmir"), ("Bayraklı", "İzmir"), ("Çiğli", "İzmir"),
    ("Gaziemir", "İzmir"), ("Balçova", "İzmir"), ("Karabağlar", "İzmir"),
    ("Menemen", "İzmir"), ("Çeşme", "İzmir"),
    # Bursa
    ("Nilüfer", "Bursa"), ("Osmangazi", "Bursa"), ("Yıldırım", "Bursa"),
    ("Gemlik", "Bursa"), ("İnegöl", "Bursa"), ("Mudanya", "Bursa"),
    # Antalya
    ("Muratpaşa", "Antalya"), ("Konyaaltı", "Antalya"), ("Kepez", "Antalya"),
    ("Alanya", "Antalya"), ("Manavgat", "Antalya"), ("Kaş", "Antalya"),
    ("Kemer", "Antalya"), ("Serik", "Antalya"), ("Finike", "Antalya"),
    # Eskişehir
    ("Tepebaşı", "Eskişehir"), ("Odunpazarı", "Eskişehir"), ("Sivrihisar", "Eskişehir"),
    # Adana
    ("Seyhan", "Adana"), ("Çukurova", "Adana"), ("Yüreğir", "Adana"), ("Ceyhan", "Adana"),
    # Konya
    ("Selçuklu", "Konya"), ("Meram", "Konya"), ("Karatay", "Konya"), ("Ereğli", "Konya"),
    # Gaziantep
    ("Şehitkamil", "Gaziantep"), ("Şahinbey", "Gaziantep"), ("Nizip", "Gaziantep"),
    # Kayseri
    ("Melikgazi", "Kayseri"), ("Kocasinan", "Kayseri"), ("Talas", "Kayseri"),
    # Mersin
    ("Yenişehir", "Mersin"), ("Toroslar", "Mersin"), ("Akdeniz", "Mersin"),
    ("Mezitli", "Mersin"), ("Tarsus", "Mersin"), ("Erdemli", "Mersin"),
    # Orta ölçekli/küçük iller -- ince kuyruk
    ("Merkez", "Bolu"), ("Göreme", "Nevşehir"), ("Ürgüp", "Nevşehir"),
    ("Adapazarı", "Sakarya"), ("Serdivan", "Sakarya"),
    ("İzmit", "Kocaeli"), ("Gebze", "Kocaeli"), ("Darıca", "Kocaeli"),
    ("Çorlu", "Tekirdağ"), ("Süleymanpaşa", "Tekirdağ"),
    ("Bodrum", "Muğla"), ("Fethiye", "Muğla"), ("Marmaris", "Muğla"), ("Milas", "Muğla"),
    ("Pamukkale", "Denizli"), ("Merkezefendi", "Denizli"),
    ("İlkadım", "Samsun"), ("Atakum", "Samsun"),
    ("Ortahisar", "Trabzon"), ("İpekyolu", "Van"),
    ("Bağlar", "Diyarbakır"), ("Kayapınar", "Diyarbakır"),
    ("Haliliye", "Şanlıurfa"), ("Eyyübiye", "Şanlıurfa"),
    ("Yeşilyurt", "Malatya"), ("Battalgazi", "Malatya"),
    ("Yakutiye", "Erzurum"), ("Merkez", "Elazığ"),
    ("Yunusemre", "Manisa"), ("Şehzadeler", "Manisa"),
    ("Karesi", "Balıkesir"), ("Altıeylül", "Balıkesir"),
    ("Efeler", "Aydın"), ("Merkez", "Isparta"), ("Merkez", "Çanakkale"),
    ("Merkez", "Edirne"), ("Merkez", "Sivas"), ("Merkez", "Kastamonu"),
    ("Altınordu", "Ordu"), ("Merkez", "Rize"), ("Merkez", "Amasya"),
    ("Merkez", "Tokat"), ("Merkez", "Çorum"), ("Merkez", "Niğde"),
    ("Merkez", "Aksaray"), ("Merkez", "Karaman"), ("Merkez", "Osmaniye"),
    ("Antakya", "Hatay"), ("İskenderun", "Hatay"),
    ("Onikişubat", "Kahramanmaraş"), ("Dulkadiroğlu", "Kahramanmaraş"),
    ("Merkez", "Kütahya"), ("Merkez", "Afyonkarahisar"), ("Merkez", "Bilecik"),
    ("Merkez", "Karabük"), ("Merkez", "Zonguldak"), ("Merkez", "Düzce"),
    ("Merkez", "Yalova"), ("Merkez", "Kırıkkale"),
]

MAHALLE_KOKLERI = [
    "Cumhuriyet", "Yeni", "Bahçelievler", "Fatih", "Barbaros", "Mimar Sinan",
    "Gazi", "Kurtuluş", "Yıldız", "Zafer", "Yeşilyurt", "Güneşli", "Çamlık",
    "Esentepe", "Değirmendere", "Reşitpaşa", "Arifiye", "Rasimpaşa", "Zühtüpaşa",
    "Cihangir", "Merkez", "Aydınlıkevler", "Güzeltepe", "Aşağı", "Yukarı",
    "İstiklal", "Atatürk", "Hürriyet", "Şirinevler", "Emek", "Sanayi",
]
MAHALLE_EKI = ["MAH.", "MH.", "MH", "Mahallesi"]

SOKAK_KOKLERI = [
    "Eski Büyükdere", "Belediye", "Karakolhane", "Fahrettin Kerim Gökay",
    "Cengiz Topel", "İstiklal", "Atatürk", "Barbaros", "İnönü", "Gül",
    "Menekşe", "Lale", "Çiçek", "Okul", "Pazar", "Değirmen", "Fevzi Çakmak",
    "Halit Ziya", "Nefit Kümeevler", "Yeşil", "Çınar", "Kavak",
]
CADDE_EKI = ["CD.", "CD", "Cadde", "Caddesi"]
SOKAK_EKI = ["SOK.", "SK.", "SOK", "Sokak"]
NO_EKI = ["NO:", "NO: ", "NO ", "N:", "NO."]

# Mevkii/AVM/numaralı-yol tipi -- 11 örnekte 2/11 (ELMALIK MÜCAVİR MEVKİİ
# HIGHWAY AVM, CİHANGİR MAH. D-100 GÜNEY YANYOL) bu türdendi.
ALTERNATIF_YAPI = [
    "{mevkii} MÜCAVİR MEVKİİ {avm} AVM",
    "D-100 GÜNEY YANYOL",
    "D-100 KUZEY YANYOL",
    "ORGANİZE SANAYİ BÖLGESİ",
]
MEVKII_ADI = ["Elmalık", "Kızılcaören", "Değirmenönü", "Kumtepe", "Karapınar"]
AVM_ADI = ["Highway", "Park", "Merkez", "City", "Forum"]

DAIRE_EKLERI = ["", "/A", "/B", "/C", "/D", "/Z1", "/21", "/1", "/2", "/13"]

# Türkçe -> ASCII harita (diyakritiksiz yazım varyasyonu için, ör. GÖKAY -> GOKAY)
_ASCII_HARITA = str.maketrans("çÇğĞıİöÖşŞüÜ", "cCgGiIoOsSuU")


def _ascii_yap(s: str) -> str:
    return s.translate(_ASCII_HARITA)


def adres_uret(rnd: random.Random) -> str:
    ilce, il = rnd.choice(IL_ILCE_HAVUZU)

    yapi_zar = rnd.random()
    if yapi_zar < 0.55:
        # (a) mahalle + cadde/sokak + no -- en yaygın
        mahalle = f"{rnd.choice(MAHALLE_KOKLERI)} {rnd.choice(MAHALLE_EKI)}"
        yol_tipi = rnd.choice(["cadde", "sokak"])
        if yol_tipi == "cadde":
            yol = f"{rnd.choice(SOKAK_KOKLERI)} {rnd.choice(CADDE_EKI)}"
        else:
            yol = f"{rnd.choice(SOKAK_KOKLERI)} {rnd.choice(SOKAK_EKI)}"
        no = f"{rnd.choice(NO_EKI)}{rnd.randint(1, 300)}{rnd.choice(DAIRE_EKLERI)}"
        govde = f"{mahalle} {yol} {no}"
    elif yapi_zar < 0.80:
        # (b) mahallesiz, yalnız cadde/sokak + no
        yol_tipi = rnd.choice(["cadde", "sokak"])
        if yol_tipi == "cadde":
            yol = f"{rnd.choice(SOKAK_KOKLERI)} {rnd.choice(CADDE_EKI)}"
        else:
            yol = f"{rnd.choice(SOKAK_KOKLERI)} {rnd.choice(SOKAK_EKI)}"
        no = f"{rnd.choice(NO_EKI)}{rnd.randint(1, 300)}{rnd.choice(DAIRE_EKLERI)}"
        govde = f"{yol} {no}"
    elif yapi_zar < 0.90:
        # (c) minimal -- yalnız ilçe (opsiyonel şehir bitiş biçiminde eklenir)
        govde = ""
    else:
        # (d) mevkii/AVM/numaralı yol
        kalip = rnd.choice(ALTERNATIF_YAPI)
        govde = kalip.format(mevkii=rnd.choice(MEVKII_ADI), avm=rnd.choice(AVM_ADI))

    # Bitiş biçimi
    bitis_zar = rnd.random()
    if bitis_zar < 0.30:
        bitis = f"{ilce}/{il}"
    elif bitis_zar < 0.55:
        bitis = f"{ilce}/ {il}"
    elif bitis_zar < 0.80:
        bitis = f"{ilce} {il}"
    else:
        bitis = ilce  # yalnız ilçe, gerçek "ÜSKÜDAR" örneği gibi

    # Posta kodu -- ÖLÇÜLMEDİ, düşük olasılık (bkz. modül docstring'i)
    posta_kodu = f"{rnd.randint(10000, 81999)} " if rnd.random() < 0.12 else ""

    adres = f"{govde} {posta_kodu}{bitis}".strip()
    adres = " ".join(adres.split())  # fazla boşlukları sadeleştir

    if rnd.random() < 0.90:
        adres = adres.upper()
    if rnd.random() < 0.12:
        adres = _ascii_yap(adres)
        adres = "".join(c for c in unicodedata.normalize("NFKD", adres) if not unicodedata.combining(c))

    return adres


def main():
    with open(KAYNAK, encoding="utf-8") as f:
        satirlar = list(csv.DictReader(f))

    rnd = random.Random(SEED)
    for satir in satirlar:
        satir["adres"] = adres_uret(rnd)

    with open(HEDEF, "w", encoding="utf-8", newline="") as f:
        yazici = csv.DictWriter(f, fieldnames=list(satirlar[0].keys()))
        yazici.writeheader()
        yazici.writerows(satirlar)

    print(f"[+] {len(satirlar)} firma için adres üretildi -> {HEDEF}")
    print("\n[+] Örnekler:")
    for satir in rnd.sample(satirlar, 8):
        print(f"    {satir['satici_unvan']:35s} {satir['adres']}")


if __name__ == "__main__":
    main()
