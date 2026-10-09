"""
BİR KEZ çalışır (Ollama kapalıyken, RAM sorunu yok): faturalar.json +
faturalar_etiketler.json'dan ~20k'lik bir alt küme örnekler, 1000'lik batch
dosyalarına böler ve bir durum.json manifesti yazar. Toplu üretim
(aciklama_toplu_uret.py) 155 MB'lık asıl dosyaya bir daha dokunmaz -- yalnızca
bu batch dosyalarını okur.

Varsayılan seçim modu ("kota"): ham anomali_turleri'ne göre tür başına
taban/tavan kotası uygular (bkz. anomali_turu_kotali_sec) -- nadir anomali
türlerinin rastgele örneklemede ezilmesini önler. Eski aciklama_kategorisi
bazlı orantılı seçim --secim-modu kategori ile hâlâ kullanılabilir.

Kullanım:
    python -m faz_b_aciklama_uretimi.batch_hazirla --toplam 20000 --batch-size 1000 --tur-taban 300 --tur-tavan 600
"""

import argparse
import json
import random
from collections import Counter
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import ILISKISEL_ANOMALILER
from ortak.cift_grup import cift_grup_anahtari

from faz_a_fis_uretimi.generators.aciklama_uretici import (
    ONCELIK_SIRASI, ACIKLAMA_KATEGORILERI, ACIKLAMA_KATEGORI_ORANLARI,
    VARSAYILAN_TEKNIK_ORAN, _belirleyici_turu_sec,
)

# ILISKISEL TURLERDE BIRIM KAYIT DEGIL OLAYDIR (2026-08-11). Enjektor ciftin iki
# uyesini de etiketledigi icin bir mukerrerlik OLAYI sette IKI kayit tutar; kotayi
# kayit cinsinden yazmak o turleri digerlerinin iki kati sisirir (olculdu: 24k
# sette mukerrer 1168, dengelenmis turler 522).
#
# Dongu tur basina bu kadar KAYIT alir, `_cift_butunlugunu_sagla` eksik esleri
# sonradan tamamlar; dolayisiyla nihai OLAY sayisi [tavan/2, tavan] araligindadir.
#
# OLCEKLEME: uretim hacmi buyuyup --tur-taban/--tur-tavan yukseltilirse BUNLAR DA
# elle yukseltilmeli. Referans oran (25k kosusu): tur 400/600 kayit iken iliskisel
# 300/400 olay, yani nihai kayit olarak dengelenmis turlerin ~1,5 kati.
ILISKISEL_TABAN = 300
ILISKISEL_TAVAN = 400

# Batch, havuzun bu oranindan fazlasini alirsa hedef kategori kompozisyonu
# tutmaz (darbogaz manipulatif; gerekce docs/dökümantasyon/nasıl-calisir.md).
GUVENLI_HAVUZ_ORANI = 0.25

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"

# Bu batch'in aciklama_kategorisi kompozisyonu için hedef -- faturalar_etiketler.
# json'daki popülasyon-geneli kalibre dağılımdan (ör. yeterli %56) BAĞIMSIZ bir
# hedef; bkz. _kategori_kotali_yeniden_ata.
VARSAYILAN_KATEGORI_HEDEF_ORANLARI: dict[str, float] = {
    "yeterli": 0.50,
    "yetersiz": 0.20,
    "manipulatif": 0.20,
    "ai_uretimi": 0.10,
}


def batch_kaydi_olustur(fatura: dict, etiket: dict, aciklama_kategorisi_override: str | None = None) -> dict:
    """Runner'ın prompt kurmak ve ihlal denetlemek için ihtiyaç duyduğu
    MİNİMUM alanları taşıyan kompakt kayıt. Asıl dosyaya bir daha dönülmez.
    aciklama_kategorisi_override verilirse, etiketteki (popülasyon geneli
    kalibre) değer yerine bu batch'e özel yeniden atanmış kategori yazılır --
    etiket dosyası DEĞİŞMEZ, sadece bu kayıtta override edilir."""
    return {
        "kayit_id": fatura["kayit_id"],   # boru hattinin BENZERSIZ anahtari
        "fatura_no": fatura["fatura_no"],
        # fatura_tarihi PROMPT'A GİRER: ai_uretimi dalı ~%15 olasılıkla "gereksiz belge
        # ayrıntısı" olarak gerçek tarihi ister (aciklama_uretim_core.py, AI ayracı).
        # Alan taşınmadığı sürece prompt'a boş değer gidiyordu ("fiş tarihi ") ve model
        # ya atlıyor ya tarih uyduruyordu -- 25k'da ~375 ai kaydını etkiliyordu.
        "fatura_tarihi": fatura["fatura_tarihi"],
        "satici_unvan": fatura["satici_unvan"],
        "kalemler": fatura["kalemler"],
        "harcama_kategorileri": etiket["harcama_kategorileri"],  # kalem siralı, prompt kurmak için gerekli
        "aciklama_kategorisi": aciklama_kategorisi_override if aciklama_kategorisi_override is not None else etiket["aciklama_kategorisi"],
        "is_anomali": etiket["is_anomali"],
        "anomali_turleri": etiket["anomali_turleri"],
    }


def dengeli_ornekle(
    kategori_havuzlari: dict[str, list[dict]],
    toplam: int,
    min_per_kategori: int,
) -> dict[str, list[dict]]:
    """
    Kategori oranlarını koruyan stratified downsample; nadir sınıflar için
    (manipulatif/ai_uretimi) bir taban (min_per_kategori) garanti eder.
    Havuzda yeterli örnek yoksa mevcut kadarını alır.
    """
    genel_toplam = sum(len(v) for v in kategori_havuzlari.values())
    secilen: dict[str, list[dict]] = {}

    # 1. adım: her kategoriye orantılı pay, ama en az min_per_kategori (havuz elverdiğince)
    for kategori, havuz in kategori_havuzlari.items():
        orantili = round(toplam * len(havuz) / genel_toplam)
        hedef = max(orantili, min_per_kategori)
        hedef = min(hedef, len(havuz))  # havuzda olandan fazlasını isteyemeyiz
        secilen[kategori] = random.sample(havuz, hedef)

    return secilen


# ---------------------------------------------------------------------------
# Kota bazlı (anomali_turleri) seçim -- 75k'lik geçerli havuzdan ~20k'lik alt
# küme çıkarırken rastgele örneklemenin nadir türleri (ör. mukerrer_fis_yukleme,
# ~1000 mevcut) ezmesini önler. dengeli_ornekle (yukarıda) aciklama_kategorisi
# (4 kova) bazında dengeler; bu fonksiyon bir kat daha temelde, ham
# anomali_turleri (11 tür, union etiketleme sonucu) bazında dengeler --
# ikisi farklı eksenler, biri diğerinin yerine geçmez ama bu, "hangi 20k
# fatura seçilsin" sorusunun asıl cevabıdır.
# ---------------------------------------------------------------------------

def _tutar_bucket(genel_toplam: float, sinirlar: tuple[float, float, float] = (500, 2000, 8000)) -> str:
    if genel_toplam < sinirlar[0]:
        return "dusuk"
    if genel_toplam < sinirlar[1]:
        return "orta"
    if genel_toplam < sinirlar[2]:
        return "yuksek"
    return "cok_yuksek"


def _cesitli_ornekle(fatura_no_listesi: list[str], fatura_map: dict[str, dict], etiket_map: dict[str, dict], n: int, rnd: random.Random) -> list[str]:
    """
    (is_kolu, tutar_bucket) katmanlarina round-robin dağıtarak n kayıt seçer --
    düz random.sample yerine, bol havuzlarda dar bir örnek deseni (ör. hep ayni
    iş kolundan/tutar araligindan) öğrenilmesin diye. n >= havuz büyüklüğü ise
    hepsini döner.
    """
    if n <= 0:
        return []
    if n >= len(fatura_no_listesi):
        return list(fatura_no_listesi)

    katmanlar: dict[tuple[str, str], list[str]] = {}
    for fno in fatura_no_listesi:
        f = fatura_map[fno]
        anahtar = (etiket_map[fno]["is_kolu"], _tutar_bucket(f["genel_toplam"]))
        katmanlar.setdefault(anahtar, []).append(fno)
    for grup in katmanlar.values():
        rnd.shuffle(grup)

    secilen: list[str] = []
    gruplar = list(katmanlar.values())
    idx = 0
    while len(secilen) < n and any(gruplar):
        grup = gruplar[idx % len(gruplar)]
        if grup:
            secilen.append(grup.pop())
        idx += 1
    return secilen


def _cesitli_sira(fatura_no_listesi: list[str], fatura_map: dict[str, dict], etiket_map: dict[str, dict], rnd: random.Random) -> list[str]:
    """_cesitli_ornekle ile ayni katman/round-robin mantığı, ama bir alt küme
    değil TÜM listenin çeşitlilik-gözeten bir sırasını döner -- çağıran taraf
    bu sırada tek tek ilerleyip her adayı ayrı ayrı kabul/red edebilsin diye
    (tavan taşma kontrolü tek tek yapılmak zorunda, bkz. anomali_turu_kotali_sec)."""
    katmanlar: dict[tuple[str, str], list[str]] = {}
    for fno in fatura_no_listesi:
        f = fatura_map[fno]
        anahtar = (etiket_map[fno]["is_kolu"], _tutar_bucket(f["genel_toplam"]))
        katmanlar.setdefault(anahtar, []).append(fno)
    for grup in katmanlar.values():
        rnd.shuffle(grup)

    sira: list[str] = []
    gruplar = list(katmanlar.values())
    idx = 0
    while any(gruplar):
        grup = gruplar[idx % len(gruplar)]
        if grup:
            sira.append(grup.pop())
        idx += 1
    return sira


def _kategori_kotali_yeniden_ata(
    secilen_no_listesi: list[str],
    etiket_map: dict[str, dict],
    hedef_oranlar: dict[str, float],
    rnd: random.Random,
) -> tuple[dict[str, str], int]:
    """
    Seçilen batch için aciklama_kategorisi'ni hedef orana (varsayılan
    %50/20/20/10) göre atar -- faturalar_etiketler.json'daki atama hiç
    DEĞİŞTİRİLMEZ, yalnızca bu batch kaydı için bir değer üretilir.

    İlk tercih HER ZAMAN faturanın etikette zaten sahip olduğu kategoridir
    (kota müsaitse hiçbir şey override edilmez). Kota doluysa, rastgele bir
    kategoriye DEĞİL, faturanın belirleyici anomali türünün (ya da temizse
    "temiz"in) ACIKLAMA_KATEGORI_ORANLARI ağırlık vektöründe büyükten
    küçüğe sıralı bir sonraki uygun kategoriye deterministik olarak düşer
    (ör. yasakli_kategori'de manipulatif dolu ise ikinci en yüksek ağırlıklı
    kategori olan yeterli'ye). Böylece anomali türü <-> açıklama kategorisi
    arasındaki araştırma-kalibreli korelasyon (ör. yasakli_kategori'nin
    "gizleme" davranışını simüle etmek için manipulatife en yatkın olması)
    override durumunda bile mümkün olduğunca korunur -- rastgele/alakasız
    bir kategoriye asla düşürülmez.
    """
    toplam = len(secilen_no_listesi)
    kalan_kota = {kategori: round(toplam * oran) for kategori, oran in hedef_oranlar.items()}
    # Yuvarlama farkını en büyük paydan telafi et (toplam kota == toplam fatura olsun)
    fark = toplam - sum(kalan_kota.values())
    if fark and kalan_kota:
        en_buyuk = max(kalan_kota, key=lambda k: kalan_kota[k])
        kalan_kota[en_buyuk] += fark

    sira = list(secilen_no_listesi)
    rnd.shuffle(sira)

    yeni_kategoriler: dict[str, str] = {}
    override_sayisi = 0

    for fno in sira:
        etiket = etiket_map[fno]
        mevcut = etiket["aciklama_kategorisi"]
        if kalan_kota.get(mevcut, 0) > 0:
            secilen_kategori = mevcut
        else:
            belirleyici_tur = _belirleyici_turu_sec(etiket["anomali_turleri"])
            agirliklar = ACIKLAMA_KATEGORI_ORANLARI.get(belirleyici_tur, VARSAYILAN_TEKNIK_ORAN)
            siralama = sorted(zip(ACIKLAMA_KATEGORILERI, agirliklar), key=lambda x: -x[1])
            secilen_kategori = next(
                (kategori for kategori, _agirlik in siralama if kalan_kota.get(kategori, 0) > 0),
                None,
            )
            if secilen_kategori is None:
                # Tüm kotalar dolu (yuvarlama kenar durumu) -- mevcut kategoride kalsin
                secilen_kategori = mevcut
            else:
                override_sayisi += 1
        yeni_kategoriler[fno] = secilen_kategori
        kalan_kota[secilen_kategori] = kalan_kota.get(secilen_kategori, 0) - 1

    return yeni_kategoriler, override_sayisi


def _konteyner_tavanlarini_hesapla(
    tur_havuzlari: dict[str, list[str]],
    tur_taban: int,
    tur_tavan: int,
    esik: float = 0.9,
) -> dict[str, int]:
    """
    Bazı anomali türleri, başka bir (çok daha büyük) türün NEREDEYSE TAM ALT
    KÜMESİDİR -- ör. genel_toplam/satir_toplami havuzlarının %100'ü aynı
    zamanda footer_kismi'ye de sahip (injector yan etkisi: toplam/satır
    tutarını bozmak doğal olarak footer tutarlılığını da bozuyor). Böyle bir
    durumda "konteyner" türün (footer_kismi) tavanı normal (tur_tavan) kalırsa,
    içindeki bağımlı türlerin taban hedeflerini AYNI ANDA karşılamak
    matematiksel olarak imkânsız olur (ör. genel_toplam(300)+satir_toplami(300)
    = 600, footer_kismi'nin tavanı da 600 ise footer'a kendi payına HİÇ yer
    kalmaz, üstelik ikisi ayrı ayrı 300'e çıkmaya çalışırken birbirini bloklar).

    Bu fonksiyon, hangi türün hangi türün konteyneri olduğunu (overlap oranı
    >= esik) veriden OTOMATİK tespit eder ve konteyner türe, içerdiği her
    bağımlı tür için tur_taban kadar EK bütçe tanır -- böylece konteyner hem
    kendi normal payını (tur_tavan) korur hem de bağımlı türlerin tabanını
    bloklamaz.
    """
    turler = list(tur_havuzlari.keys())
    havuz_seti = {tur: set(fnolar) for tur, fnolar in tur_havuzlari.items()}
    ekstra_butce: dict[str, int] = {tur: 0 for tur in turler}

    for a in turler:
        if not havuz_seti[a]:
            continue
        for b in turler:
            if a == b or len(tur_havuzlari[b]) <= len(tur_havuzlari[a]):
                continue
            oran = len(havuz_seti[a] & havuz_seti[b]) / len(havuz_seti[a])
            if oran >= esik:
                # a, (daha büyük) b'nin neredeyse tam alt kümesi -- b konteyner
                ekstra_butce[b] += tur_taban

    return {tur: tur_tavan + ekstra_butce[tur] for tur in turler}


def _cift_butunlugunu_sagla(
    secilen_anomalili: set[str],
    secilen_temiz: list[str],
    etiket_map: dict[str, dict],
    fatura_map: dict[str, dict],
    rnd: random.Random,
) -> dict:
    """Iliskisel etiketli her kaydin `(satici_vkn, fatura_no)` esini secime ekler.

    Kota secimi tur farkindali ama CIFT farkindali degildi: etiketli uyeyi bilincli
    aliyor, esi ise siradan bir TEMIZ fatura oldugu icin hicbir kotaya girmeden
    yalniz dolgu oraniyla geliyordu. Olculdu (25k): 584 mukerrer kaydin 459'u,
    366 cakisma kaydinin 294'u essiz kalmis, yani karsilastirmali bir model icin
    cozulemez hale gelmisti.

    Eklenen es tasidigi TUM turlerin kotasindan duser (tek etiketli kayitla ayni
    muhasebe; oncelik agirligi YOK, yoksa ikinci turun orani gercekten sahip
    oldugundan sapardi). Toplam sabit kalsin diye es sayisi kadar dolgu temiz
    kayit cikarilir, AYNI kategoriden -- kompozisyon kaymasin.

    Tavan denetimi bu adimda UYGULANMAZ: cift butunlugu dogruluk sarti, tavan
    ise denge sezgiseli. Asim sinirli, eslerin ~%75'i temiz ve hicbir ture
    girmiyor. Rapor asimi gorunur kilar.

    Anahtar `cift_grup.cift_grup_anahtari` (CLAUDE.md §7)."""
    anahtar: dict[tuple, list[str]] = {}
    for kid, f in fatura_map.items():
        anahtar.setdefault(cift_grup_anahtari(f), []).append(kid)

    secili = set(secilen_anomalili) | set(secilen_temiz)
    eklenecek: dict[str, str] = {}
    # Dolgudan CIKARILAMAYACAK kayitlar: secilmis iliskisel kayitlarin TUM esleri.
    # Yalniz yeni eklenenleri korumak yetmiyor -- sansa zaten secilmis bir esi
    # dolgu diye cikarinca sahibi oksuz kaliyordu (olculdu: 10 kayit).
    korunacak: set[str] = set()
    for kid in secilen_anomalili:
        if not set(etiket_map[kid]["anomali_turleri"]) & set(ILISKISEL_ANOMALILER):
            continue
        f = fatura_map[kid]
        for es in anahtar.get(cift_grup_anahtari(f), []):
            if es == kid:
                continue
            korunacak.add(es)
            if es not in secili and es not in eklenecek:
                eklenecek[es] = kid

    rapor = {"eklenen_es": len(eklenecek), "es_anomalili": 0, "cikarilan_dolgu": 0}
    if not eklenecek:
        return rapor

    # Cikarilacak dolgu, eklenen esin kategorisiyle ESLESTIRILIR; rastgele
    # cikarmak `manipulatif` gibi dar bir ekseni hedefin altina dusururdu.
    dolgu_kat: dict[str, list[str]] = {}
    for kid in secilen_temiz:
        dolgu_kat.setdefault(etiket_map[kid]["aciklama_kategorisi"], []).append(kid)
    for grup in dolgu_kat.values():
        rnd.shuffle(grup)

    cikarilacak: set[str] = set()
    for es in eklenecek:
        havuz = dolgu_kat.get(etiket_map[es]["aciklama_kategorisi"]) or []
        while havuz:
            aday = havuz.pop()
            if aday not in korunacak and aday not in cikarilacak:
                cikarilacak.add(aday)
                break

    for es in eklenecek:
        if etiket_map[es]["is_anomali"]:
            secilen_anomalili.add(es)
            rapor["es_anomalili"] += 1
        else:
            secilen_temiz.append(es)
    secilen_temiz[:] = [k for k in secilen_temiz if k not in cikarilacak]
    rapor["cikarilan_dolgu"] = len(cikarilacak)
    return rapor


# ---------------------------------------------------------------------------
# KATEGORİ KOTASININ TÜRLER ARASI ORANSAL PAYLAŞIMI (2026-08-18)
#
# ESKİ DAVRANIŞ (sorun): kategori kotası (ör. yeterli) TÜM türler ve temiz
# arasında TEK, paylaşımlı bir sayaçtı; `tur_sirasi` içindeki türler SIRAYLA
# işlenip aynı kovadan çekiyordu -- ilk işlenen tür istediği kadar alabiliyor,
# `ONCELIK_SIRASI`'nda OLMAYAN türler (ondalik_kaymasi, dusuk_ondalik_kaymasi)
# en sona düştüğü için kova onlara gelene kadar boşalmış oluyordu. Ölçüldü:
# ondalik_kaymasi'nin 120k havuzdaki yeterli oranı %58,7 iken seçilen batch'te
# %2,9'a düşüyordu (25k'lık gerçek koşuda).
#
# YENİ DAVRANIŞ: "claims problem" / "bankruptcy problem" (iktisatta kıt kaynak
# paylaşımı) literatüründeki ORANSAL (proportional) kural uygulanır. Döngü
# başlamadan ÖNCE her tür için "talep" hesaplanır (kendi tavanına kadar, kendi
# havuzundaki GERÇEK kategori dağılımı -- teorik ağırlık değil), talepler
# toplamı kovadan büyükse HERKES aynı oranda (kova/toplam_talep) kısılır. Hiç
# kimse sırf işlem sırasında geç kaldığı için SIFIRA düşmez.
#
# TEMİZ DOLGU AYRICA HESAPLANMAZ: kova, tüm türler + temiz için TEK bir
# oransal paylaşımla bölünür (temiz'in claim'i de toplam_talep'e dahil edilir,
# yoksa oran yanlış çıkar), ama temiz'in kendi payı sonuçta zaten mevcut
# "kalan_boşluğu_doldur" mekanizmasına (aşağıda değişmeden duruyor) otomatik
# düşer -- ANOMALİ türleri artık fazla tüketmediği için kalan tam olarak
# temiz'in adil payına eşitleniyor (matematiksel özdeşlik, ayrı kod gerekmez).
# ---------------------------------------------------------------------------

def _hare_en_buyuk_kalan(ham_paylar: dict[str, float], toplam: int) -> dict[str, int]:
    """Kesirli payları, toplamları TAM `toplam`'a eşit olacak şekilde tam
    sayıya yuvarlar (Hare kotası / En Büyük Kalan yöntemi -- meclis sandalye
    dağıtımında kullanılan klasik apportionment algoritması). Her pay önce
    tabana yuvarlanır, kalan birimler en büyük ondalık artığı olanlara sırayla
    dağıtılır."""
    taban = {k: int(v) for k, v in ham_paylar.items()}
    kalan = toplam - sum(taban.values())
    if kalan <= 0:
        return taban
    artik_sirasi = sorted(ham_paylar, key=lambda k: ham_paylar[k] - taban[k], reverse=True)
    for k in artik_sirasi[:kalan]:
        taban[k] += 1
    return taban


def _kategori_kotalarini_oransal_hesapla(
    tur_sirasi: list[str],
    tur_havuzlari: dict[str, list[str]],
    tavan_efektif: dict[str, int],
    etiket_map: dict[str, dict],
    kategori_hedefi: dict[str, int],
    temiz_hedef_tahmini: int,
    temiz_dogal_dagilim: "Counter[str]",
    temiz_toplam: int,
) -> dict[str, dict[str, int]]:
    """Her tür için, her `aciklama_kategorisi`de KENDİ payına düşen kotayı
    döner: {tur: {kategori: kota}}. Yöntem yukarıdaki modül notunda anlatılan
    oransal (proportional) claims-problem kuralı."""
    kategoriler = list(ACIKLAMA_KATEGORILERI)
    tur_kategori_kotasi: dict[str, dict[str, int]] = {tur: {} for tur in tur_sirasi}

    for kategori in kategoriler:
        kova = kategori_hedefi.get(kategori, 0)

        talepler: dict[str, float] = {}
        for tur in tur_sirasi:
            havuzdaki = sum(
                1 for fno in tur_havuzlari[tur]
                if etiket_map[fno]["aciklama_kategorisi"] == kategori
            )
            talepler[tur] = min(tavan_efektif.get(tur, 0), havuzdaki)

        # TEMİZ de bir "talip": claim'i toplam_talep'e girmezse oran yanlış
        # hesaplanır (bkz. modül notu). Kendi payı ayrıca saklanmaz -- mevcut
        # kalan-boşluk mekanizması onu zaten örtük olarak verir.
        if temiz_toplam > 0:
            temiz_dogal_oran = temiz_dogal_dagilim.get(kategori, 0) / temiz_toplam
            talepler["__temiz__"] = min(
                temiz_dogal_dagilim.get(kategori, 0),
                temiz_hedef_tahmini * temiz_dogal_oran,
            )
        else:
            talepler["__temiz__"] = 0.0

        toplam_talep = sum(talepler.values())
        if toplam_talep <= kova or toplam_talep == 0:
            # Kıtlık yok -- herkes talebini tam alır (tam sayıya yuvarlanır,
            # kova zaten talebi karşılıyor, asirim gerekmez).
            paylar = {tur: int(round(talepler[tur])) for tur in tur_sirasi}
        else:
            oran = kova / toplam_talep
            ham_paylar = {tur: talepler[tur] * oran for tur in tur_sirasi}
            # Yalniz TUR'lerin payi lazim (temiz kalan-bosluktan kendiliginden
            # cikiyor) -- ama Hare toplami dogru cikmasi icin temiz'i de
            # yuvarlama havuzuna katip sonra atiyoruz.
            ham_paylar["__temiz__"] = talepler["__temiz__"] * oran
            tam_paylar = _hare_en_buyuk_kalan(ham_paylar, kova)
            paylar = {tur: tam_paylar[tur] for tur in tur_sirasi}

        for tur in tur_sirasi:
            tur_kategori_kotasi[tur][kategori] = paylar[tur]

    return tur_kategori_kotasi


def anomali_turu_kotali_sec(
    faturalar: list[dict],
    etiketler: list[dict],
    hedef_toplam: int = 20000,
    tur_taban: int = 300,
    tur_tavan: int = 600,
    iliskisel_taban: int = ILISKISEL_TABAN,
    iliskisel_tavan: int = ILISKISEL_TAVAN,
    temiz_orani_min: float = 0.70,
    temiz_orani_max: float = 0.75,
    hedef_kategori_oranlari: dict[str, float] | None = None,
    kategori_override: bool = False,
    kategori_hedefli: bool = True,
    seed: int = 42,
) -> tuple[list[dict], dict]:
    """
    Fatura havuzundan, her anomali türü (ham `anomali_turleri`) alt kümede en az
    `tur_taban`, en fazla `tur_tavan` kadar temsil edilecek şekilde kota bazlı
    seçim yapar. Çoklu etiketli bir fatura seçildiğinde sahip olduğu TÜM
    türlerin kotasından düşülür (faturayı iki kez saymadan, kotayı şişirmeden);
    kalan hedef temiz faturalarla (~%70-75) doldurulur.

    İkinci ve bağımsız eksen `aciklama_kategorisi`dir (varsayılan hedef
    %50/20/20/10): etiket YENİDEN ATANMAZ, o kategoriye sahip faturalar SEÇİLİR
    (`kategori_hedefli`). Eski yeniden-atama yolu `kategori_override` ile açılır.

    Döner: (secilenler, rapor). Rapor tür bazlı seçim adetlerini, tabanın
    altında kalan türleri, nihai anomali oranını (bandın dışına çıkarsa
    işaretler, zorlamaz) ve kategori dağılımını içerir.
    Ölçümler/gerekçe: docs/dökümantasyon/02-faz-b-aciklama-uretimi.md
    """
    if hedef_kategori_oranlari is None:
        hedef_kategori_oranlari = VARSAYILAN_KATEGORI_HEDEF_ORANLARI
    rnd = random.Random(seed)

    # DİKKAT: anahtar fatura_no DEĞİL kayit_id -- fatura_no mükerrer anomalilerde
    # kasitli tekrar eder, onunla anahtarlarsak çiftin biri sessizce DÜŞER.
    etiket_map = {e["kayit_id"]: e for e in etiketler}
    fatura_map = {f["kayit_id"]: f for f in faturalar}

    anomalili_no = [fno for fno, e in etiket_map.items() if e["is_anomali"]]
    temiz_no = [fno for fno, e in etiket_map.items() if not e["is_anomali"]]

    # Tür havuzları sabit liste yerine etiketlerde fiilen görülen türlerden
    # dinamik kurulur (yeni bir anomali türü eklenirse otomatik dahil olur).
    tur_havuzlari: dict[str, list[str]] = {}
    for fno in anomalili_no:
        for tur in etiket_map[fno]["anomali_turleri"]:
            tur_havuzlari.setdefault(tur, []).append(fno)

    # İşlem sırası ONCELIK_SIRASI'nı (aciklama_uretici) yeniden kullanır: türler
    # iç içe geçtiği için (ör. genel_toplam ⊂ footer_kismi) ortak tavan bütçesini
    # önce işlenen alır, ONCELIK_SIRASI da en kasıtlı türü öne koyar. Listede
    # olmayan yeni tür, havuzu küçük olan önce gelecek şekilde sona eklenir.
    bilinen_turler = [t for t in ONCELIK_SIRASI if t in tur_havuzlari]
    yeni_turler = sorted(
        (t for t in tur_havuzlari if t not in ONCELIK_SIRASI),
        key=lambda t: len(tur_havuzlari[t]),
    )
    tur_sirasi = bilinen_turler + yeni_turler

    # Konteyner türlere (ör. footer_kismi) veriden otomatik ek tavan bütçesi.
    tavan_efektif = _konteyner_tavanlarini_hesapla(tur_havuzlari, tur_taban, tur_tavan)
    # Iliskisel turlerde birim OLAY (bkz. ILISKISEL_TAVAN). Ciftin IKI uyesi de
    # etiketli oldugu icin bir olay havuzda 2 kayit tutar; dongunun kayit tavani
    # bu yuzden 2x. Olculdu: 1x verilince nihai olay tavanin yarisinda kaliyordu.
    for _t in ILISKISEL_ANOMALILER:
        if _t in tavan_efektif:
            tavan_efektif[_t] = 2 * iliskisel_tavan

    # tur_sayaci: bir türden GERÇEKTE kaç fatura seçildiği (union nedeniyle başka
    # bir türün turunda seçilse de sayılır). Tavan bunun üzerinden KATI uygulanır;
    # aksi halde yapısal türler union'a sızıp tavanı fazlasıyla aşardı.
    tur_sayaci: dict[str, int] = {tur: 0 for tur in tur_havuzlari}
    secilen_anomalili: set[str] = set()

    # --- KATEGORİ EKSENİ: yeniden atama DEĞİL, SEÇİM ---------------------
    # aciklama_kategorisi Faz A'da kalibre edilir, BURADA ASLA DEĞİŞTİRİLMEZ; hedef
    # kompozisyona o kategoriye SAHİP faturaları seçerek ulaşılır (salt tür kotası
    # kategoriye kör, kompozisyon havuzun doğal dağılımına düşüyordu).
    kategori_hedefi: dict[str, int] = {
        kat: round(hedef_toplam * oran) for kat, oran in hedef_kategori_oranlari.items()
    } if kategori_hedefli else {}
    kategori_sayaci: Counter = Counter()

    # KITLIK SIRASI: anomalili turda önceliği, TEMİZ havuzu en KÜÇÜK olan kategori
    # alır. Anomalili slotlar kıt, temiz havuz dolgudur: temizde bol olanı (yeterli)
    # anomalili slotta harcamak israf, kıtı (manipulatif) ancak oradan toplanabilir.
    _temiz_bolluk: Counter = Counter(
        etiket_map[fno]["aciklama_kategorisi"] for fno in temiz_no
    )
    kategori_kitlik_sirasi: dict[str, int] = {
        kat: sira for sira, kat in enumerate(sorted(_temiz_bolluk, key=lambda k: _temiz_bolluk[k]))
    }
    _DOLMUS = 99  # kotası dolmuş kategori en sona

    # TÜR BAŞINA ORANSAL KATEGORİ KOTASI (bkz. modül başındaki not): her tür,
    # "yeterli" gibi bol kategorilerden SIRAYA değil kendi ADİL PAYINA göre
    # alır -- ONCELIK_SIRASI dışı türler (ondalik_kaymasi vb.) artık sırf en
    # son işlendiği için sıfıra düşmüyor. temiz_hedef henüz kesin değil (anomali
    # döngüsü bitmeden bilinmiyor); temiz_orani_min ile MUHAFAZAKAR bir tahmin
    # yeterli -- yalnız claim büyüklüğü için kullanılıyor, nihai seçim etkilenmez.
    _temiz_hedef_tahmini = int(hedef_toplam * temiz_orani_min)
    tur_kategori_kotasi = _kategori_kotalarini_oransal_hesapla(
        tur_sirasi, tur_havuzlari, tavan_efektif, etiket_map, kategori_hedefi,
        _temiz_hedef_tahmini, _temiz_bolluk, len(temiz_no),
    ) if kategori_hedefli else {tur: {} for tur in tur_sirasi}
    tur_kategori_sayaci: dict[str, Counter] = {tur: Counter() for tur in tur_sirasi}

    def _kategori_onceligi(fno: str, tur: str) -> int:
        """Küçük = önce seç. Kota KATI sınır DEĞİL: dolmuş kategori yalnız
        sıralamada sona düşer, aksi halde tür tabanları karşılanamazdı. Kota
        artık GLOBAL değil, `tur`ün KENDİ oransal payı -- bkz. modül başı notu."""
        if not kategori_hedefli:
            return 0
        kat = etiket_map[fno]["aciklama_kategorisi"]
        if tur_kategori_sayaci[tur][kat] >= tur_kategori_kotasi[tur].get(kat, 0):
            return _DOLMUS
        return kategori_kitlik_sirasi.get(kat, _DOLMUS - 1)

    for tur in tur_sirasi:
        ihtiyac = tavan_efektif[tur] - tur_sayaci[tur]
        if ihtiyac <= 0:
            continue
        adaylar = [fno for fno in tur_havuzlari[tur] if fno not in secilen_anomalili]
        sira = _cesitli_sira(adaylar, fatura_map, etiket_map, rnd)
        # Sıralama İKİ eksenli: önce kategori kotası açık olanlar (kompozisyon),
        # sonra münhasırlık -- başka türlere daha az sahip aday önce denenir ki kıt
        # bütçeli başka türün kotası gereksiz tüketilmesin. Kategori ÖNCE gelir:
        # tür tavanı zaten katı sınır, münhasırlık yalnız optimizasyon.
        sira.sort(key=lambda fno: (
            _kategori_onceligi(fno, tur),
            sum(1 for t2 in etiket_map[fno]["anomali_turleri"] if t2 != tur and t2 in tur_sayaci),
        ))
        # İKİ GEÇİŞLİ seçim (2026-08-18 düzeltmesi): sıralama TEK BAŞINA kotayı
        # zorlamaz -- sıra baştan sona tek seferde taranıp ilk `ihtiyac` kadarı
        # alınırsa, kıtlık sırasında ÖNDE olan (manipulatif/ai_uretimi/yetersiz)
        # havuzu tek başına `ihtiyac`ı doldurabilir ve `yeterli`nin sırası HİÇ
        # gelmez (ölçüldü: ondalik_kaymasi'nda kota 363 iken fiilen 0 seçiliyordu,
        # çünkü diğer üç kategori zaten 572'lik ihtiyacı dolduruyordu). Bu yüzden
        # 1. geçiş kotayı GERÇEK bir sınır olarak kontrol eder (aday kendi
        # kategorisinde hâlâ açık kota varsa alınır, yoksa ERTELENİR); 2. geçiş
        # (dolgu) yalnız `ihtiyac` hâlâ karşılanmadıysa ertelenenlerden tamamlar
        # -- kota "KATI sınır DEĞİL" ilkesi (tür tabanı her zaman önceliklidir).
        alinan = 0
        ertelenen: list[str] = []
        for fno in sira:
            if alinan >= ihtiyac:
                break
            turleri = etiket_map[fno]["anomali_turleri"]
            # Bu faturayi almak BAŞKA bir türü kendi efektif tavanının üzerine
            # taşıyorsa vazgeç -- tavan her tür için bağımsızdır.
            if any(tur_sayaci[t2] >= tavan_efektif[t2] for t2 in turleri if t2 != tur and t2 in tur_sayaci):
                continue
            kat = etiket_map[fno]["aciklama_kategorisi"]
            if kategori_hedefli and tur_kategori_sayaci[tur][kat] >= tur_kategori_kotasi[tur].get(kat, 0):
                ertelenen.append(fno)
                continue
            secilen_anomalili.add(fno)
            kategori_sayaci[kat] += 1
            tur_kategori_sayaci[tur][kat] += 1
            for t2 in turleri:
                if t2 in tur_sayaci:
                    tur_sayaci[t2] += 1
            alinan += 1

        if alinan < ihtiyac and ertelenen:
            for fno in ertelenen:
                if alinan >= ihtiyac:
                    break
                turleri = etiket_map[fno]["anomali_turleri"]
                if any(tur_sayaci[t2] >= tavan_efektif[t2] for t2 in turleri if t2 != tur and t2 in tur_sayaci):
                    continue
                kat = etiket_map[fno]["aciklama_kategorisi"]
                secilen_anomalili.add(fno)
                kategori_sayaci[kat] += 1
                tur_kategori_sayaci[tur][kat] += 1
                for t2 in turleri:
                    if t2 in tur_sayaci:
                        tur_sayaci[t2] += 1
                alinan += 1

    # Küçük bir hedef_toplam'da tür kotalarının toplamı hedefin tamamını doldurup
    # temize yer bırakmayabilir; anomalili küme hedef bandın tavanına, çeşitlilik
    # korunarak kırpılır (kırpma olduysa rapor işaretler).
    anomali_ust_sinir = int(hedef_toplam * (1 - temiz_orani_min))
    anomalili_kirpildi = len(secilen_anomalili) > anomali_ust_sinir
    if anomalili_kirpildi:
        # Kırparken kategori hedefini gözet: kotası AÇIK olanı koru, fazlalığı
        # doymuş kategoriden at (rastgele kırpma manipulatifi hedefin altına atardı).
        kirpma_sirasi = _cesitli_sira(list(secilen_anomalili), fatura_map, etiket_map, rnd)
        if kategori_hedefli:
            tutulan: Counter = Counter()

            # Kırpma TEK eksende kalmalı; nadirlik ikinci anahtar olarak denendi ve
            # geri alındı (tek-etiketli türleri sona atıp çökertiyordu).
            def _kirpma_anahtari(fno: str) -> int:
                kat = etiket_map[fno]["aciklama_kategorisi"]
                tutulan[kat] += 1
                return 0 if tutulan[kat] <= kategori_hedefi.get(kat, 0) else 1

            kirpma_sirasi.sort(key=_kirpma_anahtari)
        secilen_anomalili = set(kirpma_sirasi[:max(anomali_ust_sinir, 0)])
        kategori_sayaci = Counter(
            etiket_map[fno]["aciklama_kategorisi"] for fno in secilen_anomalili
        )

    anomalili_sayisi = len(secilen_anomalili)
    temiz_hedef = max(0, min(hedef_toplam - anomalili_sayisi, len(temiz_no)))

    if kategori_hedefli:
        # TEMİZ doldurma da kategori KATMANLI: her kategoride kalan açık kadar temiz
        # çekilir, havuz yetmezse zorlanmaz (eksik rapora yazılır).
        temiz_kat: dict[str, list[str]] = {}
        for fno in temiz_no:
            temiz_kat.setdefault(etiket_map[fno]["aciklama_kategorisi"], []).append(fno)
        secilen_temiz = []
        # Dar kategoriler (manipulatif) ÖNCE: geniş olanlar toplam kotayı kapmasın.
        for kat in sorted(temiz_kat, key=lambda k: len(temiz_kat[k])):
            acik = kategori_hedefi.get(kat, 0) - kategori_sayaci[kat]
            if acik <= 0:
                continue
            alinacak = min(acik, temiz_hedef - len(secilen_temiz))
            if alinacak <= 0:
                continue
            pay = _cesitli_ornekle(temiz_kat[kat], fatura_map, etiket_map, alinacak, rnd)
            secilen_temiz.extend(pay)
            kategori_sayaci[kat] += len(pay)
        # Hedefler karşılandıktan sonra yer kaldıysa kalanı doğal dağılımdan tamamla.
        if len(secilen_temiz) < temiz_hedef:
            kalanlar = [f for f in temiz_no if f not in set(secilen_temiz)]
            secilen_temiz.extend(
                _cesitli_ornekle(kalanlar, fatura_map, etiket_map, temiz_hedef - len(secilen_temiz), rnd)
            )
    else:
        secilen_temiz = _cesitli_ornekle(temiz_no, fatura_map, etiket_map, temiz_hedef, rnd)

    cift_raporu = _cift_butunlugunu_sagla(
        secilen_anomalili, secilen_temiz, etiket_map, fatura_map, rnd
    )
    anomalili_sayisi = len(secilen_anomalili)

    tum_secilen_no = list(secilen_anomalili) + secilen_temiz
    rnd.shuffle(tum_secilen_no)

    # KATEGORİ OVERRIDE VARSAYILAN KAPALI: etiketi yeniden atamak Faz A'nın kalibre
    # anomali↔kategori korelasyonunu bozar, ayrıca yeni kategori etiket dosyasına
    # geri yazılmadığı için metin ile etiket çelişirdi (--kategori-override ile açılır).
    if kategori_override:
        yeni_kategoriler, kategori_override_sayisi = _kategori_kotali_yeniden_ata(
            tum_secilen_no, etiket_map, hedef_kategori_oranlari, rnd
        )
    else:
        yeni_kategoriler = {fno: etiket_map[fno]["aciklama_kategorisi"] for fno in tum_secilen_no}
        kategori_override_sayisi = 0
    secilenler = [
        batch_kaydi_olustur(fatura_map[fno], etiket_map[fno], aciklama_kategorisi_override=yeni_kategoriler[fno])
        for fno in tum_secilen_no
    ]

    toplam = len(tum_secilen_no)
    anomali_orani = anomalili_sayisi / toplam if toplam else 0.0
    hedef_araligi = (round(1 - temiz_orani_max, 4), round(1 - temiz_orani_min, 4))

    tur_raporu: dict[str, dict] = {}
    hedefin_altinda_kalanlar: list[str] = []
    for tur, havuz in tur_havuzlari.items():
        secilen_bu_tur = sum(1 for fno in secilen_anomalili if tur in etiket_map[fno]["anomali_turleri"])
        _taban = 2 * iliskisel_taban if tur in ILISKISEL_ANOMALILER else tur_taban
        yetersiz = secilen_bu_tur < _taban
        if yetersiz:
            hedefin_altinda_kalanlar.append(tur)
        tur_raporu[tur] = {
            "mevcut_havuzda": len(havuz),
            "secilen": secilen_bu_tur,
            "taban": _taban,
            "tavan": tavan_efektif[tur],
            "konteyner": tavan_efektif[tur] > tur_tavan,
            "hedefin_altinda": yetersiz,
        }

    # Nihai aciklama_kategorisi dağılımı (override açıksa onun sonucu).
    kategori_sayaci = Counter(s["aciklama_kategorisi"] for s in secilenler)
    kategori_raporu = {
        kategori: {"adet": adet, "oran": round(adet / toplam, 4) if toplam else 0.0}
        for kategori, adet in kategori_sayaci.items()
    }

    rapor = {
        "toplam_secilen": toplam,
        "havuz_boyutu": len(faturalar),
        "havuz_orani": round(toplam / len(faturalar), 4) if faturalar else 0.0,
        "anomalili_sayisi": anomalili_sayisi,
        "anomalili_kirpildi": anomalili_kirpildi,
        "temiz_sayisi": len(secilen_temiz),
        "anomali_orani": anomali_orani,
        "hedef_anomali_orani_araligi": hedef_araligi,
        "aciklama_kategorisi_hedef_oranlari": hedef_kategori_oranlari,
        "aciklama_kategorisi_override_sayisi": kategori_override_sayisi,
        "anomali_orani_bandin_disinda": not (hedef_araligi[0] <= anomali_orani <= hedef_araligi[1]),
        "tur_bazli": tur_raporu,
        "hedefin_altinda_kalan_turler": hedefin_altinda_kalanlar,
        "aciklama_kategorisi_dagilimi": kategori_raporu,
        "cift_butunlugu": cift_raporu,
    }
    return secilenler, rapor


def _kota_raporu_yazdir(rapor: dict) -> None:
    print("\n[+] Tür bazlı kota raporu:")
    for tur, bilgi in sorted(rapor["tur_bazli"].items(), key=lambda x: -x[1]["mevcut_havuzda"]):
        isaret = "  [HEDEF ALTINDA]" if bilgi["hedefin_altinda"] else ""
        konteyner_etiketi = "  [KONTEYNER]" if bilgi["konteyner"] else ""
        print(
            f"      {tur:32s}: secilen={bilgi['secilen']:4d}  "
            f"(havuz={bilgi['mevcut_havuzda']:5d}, taban={bilgi['taban']}, tavan={bilgi['tavan']}){konteyner_etiketi}{isaret}"
        )
    if rapor["hedefin_altinda_kalan_turler"]:
        print(f"\n[!] Tabanin altinda kalan turler: {', '.join(rapor['hedefin_altinda_kalan_turler'])}")
    if rapor["anomalili_kirpildi"]:
        print("\n[!] hedef_toplam kucuk kaldigi icin anomalili kume temiz oranini korumak adina kirpildi.")
    print(
        f"\n[+] Toplam secilen: {rapor['toplam_secilen']}  "
        f"(anomalili={rapor['anomalili_sayisi']}, temiz={rapor['temiz_sayisi']})"
    )
    bant = rapor["hedef_anomali_orani_araligi"]
    uyari = " [!] BANDIN DISINDA" if rapor["anomali_orani_bandin_disinda"] else ""
    print(f"[+] Anomali orani: {rapor['anomali_orani']:.3f}  (hedef bant: {bant[0]:.2f}-{bant[1]:.2f}){uyari}")

    cift = rapor["cift_butunlugu"]
    print(
        f"\n[+] Cift butunlugu: {cift['eklenen_es']} es eklendi "
        f"({cift['es_anomalili']} anomalili, {cift['eklenen_es'] - cift['es_anomalili']} temiz); "
        f"yerine {cift['cikarilan_dolgu']} dolgu temiz kayit cikarildi."
    )

    hedef_kat = rapor["aciklama_kategorisi_hedef_oranlari"]
    print("\n[+] aciklama_kategorisi dagilimi (secilen alt kumede, hedefle kiyasla):")
    for kategori, bilgi in sorted(rapor["aciklama_kategorisi_dagilimi"].items(), key=lambda x: -x[1]["adet"]):
        hedef_oran = hedef_kat.get(kategori, 0.0)
        print(
            f"      {kategori:12s}: {bilgi['adet']:5d}  (%{bilgi['oran']*100:5.1f}, hedef %{hedef_oran*100:.0f})"
        )
    # Hedef kompozisyonun darbogazi manipulatif: havuzun ~%6,5'i manipulatif ve
    # secim bunun ancak ~%78'ini cikarabiliyor, batch ise %20 istiyor. Batch
    # havuzun dortte birini gecince yetismiyor (olculdu: %25 -> %19,8, %30 ->
    # %16,9, %40 -> %12,5). Sessiz kalirsa sapma ancak gozle fark edilir.
    if rapor["havuz_orani"] > GUVENLI_HAVUZ_ORANI:
        print(
            f"\n[!] Batch havuzun %{rapor['havuz_orani']*100:.0f}'ini aliyor "
            f"(guvenli tavan %{GUVENLI_HAVUZ_ORANI*100:.0f}): "
            f"{rapor['toplam_secilen']} / {rapor['havuz_boyutu']}.\n"
            f"    Manipulatif hedefin altinda kalabilir. Havuzu buyut ya da "
            f"--toplam'i dusur; tablo icin docs/dökümantasyon/nasıl-calisir.md."
        )

    toplam_secilen = rapor["toplam_secilen"]
    override_orani = rapor["aciklama_kategorisi_override_sayisi"] / toplam_secilen if toplam_secilen else 0.0
    print(
        f"\n[+] Kategori override: {rapor['aciklama_kategorisi_override_sayisi']} fatura "
        f"(%{override_orani*100:.1f}) -- kendi kategorisinin kotasi dolu oldugu icin "
        f"turunun agirlik sirasindaki bir sonraki kategoriye dustu."
    )


def main():
    parser = argparse.ArgumentParser(description="Dengeli alt küme örnekle ve batch dosyalarına böl")
    parser.add_argument("--input-json", default="data/faturalar.json", help="faturalar.json yolu")
    parser.add_argument("--etiket-json", default="data/faturalar_etiketler.json", help="etiketler.json yolu")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="Batch dosyalarının yazılacağı dizin")
    parser.add_argument("--toplam", type=int, default=22000, help="Hedef toplam fatura sayısı (yaklaşık)")
    parser.add_argument("--batch-size", type=int, default=1000, help="Her batch dosyasındaki fatura sayısı")
    parser.add_argument(
        "--secim-modu", choices=["kota", "kategori"], default="kota",
        help="kota: anomali_turleri bazli tur-kotali secim (varsayilan, nadir turleri korur); "
             "kategori: eski aciklama_kategorisi bazli orantili secim",
    )
    parser.add_argument("--min-per-kategori", type=int, default=2500, help="[kategori modu] Nadir sınıflar için taban örnek sayısı")
    parser.add_argument("--tur-taban", type=int, default=300, help="[kota modu] Anomali türü başına hedeflenen taban")
    parser.add_argument("--tur-tavan", type=int, default=600, help="[kota modu] Anomali türü başına izin verilen tavan")
    parser.add_argument("--iliskisel-taban", type=int, default=ILISKISEL_TABAN,
                        help="[kota modu] Iliskisel turlerde taban -- birim OLAY (cift), kayit degil")
    parser.add_argument("--iliskisel-tavan", type=int, default=ILISKISEL_TAVAN,
                        help="[kota modu] Iliskisel turlerde tavan -- birim OLAY (cift), kayit degil")
    parser.add_argument("--temiz-orani-min", type=float, default=0.70, help="[kota modu] Alt kümede hedeflenen minimum temiz oranı")
    parser.add_argument("--temiz-orani-max", type=float, default=0.75, help="[kota modu] Alt kümede hedeflenen maksimum temiz oranı")
    parser.add_argument("--kategori-oran-yeterli", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["yeterli"], help="[kota modu] aciklama_kategorisi hedef oranı: yeterli")
    parser.add_argument("--kategori-oran-yetersiz", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["yetersiz"], help="[kota modu] aciklama_kategorisi hedef oranı: yetersiz")
    parser.add_argument("--kategori-oran-manipulatif", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["manipulatif"], help="[kota modu] aciklama_kategorisi hedef oranı: manipulatif")
    parser.add_argument("--kategori-oran-ai-uretimi", type=float, default=VARSAYILAN_KATEGORI_HEDEF_ORANLARI["ai_uretimi"], help="[kota modu] aciklama_kategorisi hedef oranı: ai_uretimi")
    parser.add_argument(
        "--kategori-override", action="store_true",
        help="[kota modu] aciklama_kategorisi'ni --kategori-oran-* hedefine göre YENİDEN ATA. "
             "VARSAYILAN KAPALI: override, aciklama_uretici'nin kalibre edilmiş anomali↔kategori "
             "korelasyonunu bozar ve etiket dosyasına geri yazılmadığı için metinle çelişir.",
    )
    parser.add_argument(
        "--kategori-hedefsiz", action="store_true",
        help="[kota modu] Kategori-farkındalı SEÇİMİ kapat. Varsayılan AÇIK: hedef "
             "kompozisyona (--kategori-oran-*) etiketi DEĞİŞTİRMEDEN, o kategoriye "
             "sahip faturaları seçerek ulaşılır. Kapatılırsa kompozisyon havuzun "
             "doğal dağılımına düşer (ölçüldü, 20k: %%56/30/7/8).",
    )
    parser.add_argument("--seed", type=int, default=42, help="Tekrarlanabilirlik için rastgelelik tohumu")
    args = parser.parse_args()

    random.seed(args.seed)

    cikti_dizini = Path(args.cikti_dizini)
    cikti_dizini.mkdir(parents=True, exist_ok=True)

    print(f"[+] {args.input_json} okunuyor (büyük dosya, biraz sürebilir)...")
    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)
    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)

    etiket_map = {e["fatura_no"]: e for e in etiketler}
    if "aciklama_kategorisi" not in etiketler[0]:
        print("HATA: etiketler.json'da aciklama_kategorisi yok.")
        return

    if args.secim_modu == "kota":
        hedef_kategori_oranlari = {
            "yeterli": args.kategori_oran_yeterli,
            "yetersiz": args.kategori_oran_yetersiz,
            "manipulatif": args.kategori_oran_manipulatif,
            "ai_uretimi": args.kategori_oran_ai_uretimi,
        }
        secilenler, rapor = anomali_turu_kotali_sec(
            faturalar, etiketler,
            hedef_toplam=args.toplam,
            tur_taban=args.tur_taban,
            tur_tavan=args.tur_tavan,
            iliskisel_taban=args.iliskisel_taban,
            iliskisel_tavan=args.iliskisel_tavan,
            temiz_orani_min=args.temiz_orani_min,
            temiz_orani_max=args.temiz_orani_max,
            hedef_kategori_oranlari=hedef_kategori_oranlari,
            kategori_override=args.kategori_override,
            kategori_hedefli=not args.kategori_hedefsiz,
            seed=args.seed,
        )
        _kota_raporu_yazdir(rapor)
    else:
        # Kategori bazında havuzları kur (pilot'taki join mantığının aynısı)
        kategori_havuzlari: dict[str, list[dict]] = {}
        for fatura in faturalar:
            etiket = etiket_map.get(fatura["kayit_id"])
            if etiket is None:
                continue
            kategori_havuzlari.setdefault(etiket["aciklama_kategorisi"], []).append(fatura)

        print("[+] Havuz dağılımı (tüm veri):")
        for k, v in sorted(kategori_havuzlari.items(), key=lambda x: -len(x[1])):
            print(f"      {k:12s}: {len(v)}")

        secilen_kategori = dengeli_ornekle(kategori_havuzlari, args.toplam, args.min_per_kategori)

        print("\n[+] Seçilen alt küme dağılımı:")
        secilenler = []
        for kategori, faturalar_alt in secilen_kategori.items():
            print(f"      {kategori:12s}: {len(faturalar_alt)}")
            for fatura in faturalar_alt:
                secilenler.append(batch_kaydi_olustur(fatura, etiket_map[fatura["kayit_id"]]))

        random.shuffle(secilenler)

    print(f"\n[+] Toplam seçilen: {len(secilenler)} fatura")

    # Batch dosyalarına böl
    batch_manifest = []
    batch_no = 0
    for i in range(0, len(secilenler), args.batch_size):
        batch_no += 1
        dilim = secilenler[i : i + args.batch_size]
        batch_dosya = f"batch_{batch_no:04d}.json"
        cikti_dosya = f"batch_{batch_no:04d}_ciktilar.json"
        with open(cikti_dizini / batch_dosya, "w", encoding="utf-8") as f:
            # indent=2: faturalar.json ile aynı biçim (main.py). Tek satırlık JSON
            # editörde inceleme sırasında donduruyordu -- açıklama üretimini teşhis
            # ederken aynı faturaya bakmak yaygın bir iş, okunabilirlik önemli.
            json.dump(dilim, f, ensure_ascii=False, indent=2)
        batch_manifest.append({
            "dosya": batch_dosya,
            "cikti_dosyasi": cikti_dosya,
            "adet": len(dilim),
            "tamam": False,
        })

    durum = {
        "config": {
            "toplam_hedef": args.toplam,
            "batch_size": args.batch_size,
            "secim_modu": args.secim_modu,
            "min_per_kategori": args.min_per_kategori,
            "tur_taban": args.tur_taban,
            "tur_tavan": args.tur_tavan,
            "iliskisel_taban": args.iliskisel_taban,
            "iliskisel_tavan": args.iliskisel_tavan,
            "temiz_orani_min": args.temiz_orani_min,
            "temiz_orani_max": args.temiz_orani_max,
            "kategori_oran_yeterli": args.kategori_oran_yeterli,
            "kategori_oran_yetersiz": args.kategori_oran_yetersiz,
            "kategori_oran_manipulatif": args.kategori_oran_manipulatif,
            "kategori_oran_ai_uretimi": args.kategori_oran_ai_uretimi,
            "seed": args.seed,
        },
        "toplam_secilen": len(secilenler),
        "batch_sayisi": len(batch_manifest),
        "batchler": batch_manifest,
    }
    if args.secim_modu == "kota":
        durum["kota_raporu"] = rapor
    with open(cikti_dizini / "durum.json", "w", encoding="utf-8") as f:
        json.dump(durum, f, ensure_ascii=False, indent=2)

    print(f"[+] {len(batch_manifest)} batch dosyası + durum.json yazıldı -> {cikti_dizini}/")
    print(f"[+] Sonraki adım: python -m faz_b_aciklama_uretimi.aciklama_toplu_uret")


if __name__ == "__main__":
    main()
