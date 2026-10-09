"""
Açıklama üretiminin paylaşılan çekirdeği: prompt kurulumu, Ollama çağrısı,
kural tabanlı ihlal tespiti ve düzeltici retry mantığı burada tek kaynak
olarak durur. Hem pilot (aciklama_llm_pilot.py) hem toplu üretim
(aciklama_toplu_uret.py) bu modülü kullanır -- böylece prompt/retry mantığı
asla ayrışmaz.
"""

import os
import random
import re
import threading
import unicodedata
import time
import requests
from collections import Counter
from pathlib import Path
from ortak.cift_grup import cift_grup_anahtari

OLLAMA_HOST_VARSAYILAN = "http://localhost:11434"
MODEL_VARSAYILAN = "qwen3:8b"

# ---------------------------------------------------------------------------
# MODEL PROFİLLERİ -- modelin Ollama'ya nasıl konuşulacağını belirler.
# ---------------------------------------------------------------------------
# Neden gerekli: qwen3:8b Ollama'nın KENDİ kütüphanesinden geliyor; şablonu,
# düşünme anahtarı ve stop token'ları küratörlenmiş bir Modelfile'da duruyor,
# `think: false` çalışıyor. HF'den (`hf.co/...`) çekilen GGUF'ta ise yalnızca
# dosya var: şablon, kuantize edenin gömdüğü düz ChatML olabilir ve Qwen3'ün
# `enable_thinking` mantığını İÇERMEZ. O durumda `think: false` sessizce etkisiz
# kalır (hata da vermez), `/no_think` direktifi de sadece bir metin parçası olur;
# model muhakemeyi ETİKETSİZ biçimde doğrudan cevaba döker.
#
# Trendyol-LLM-8B-T1'de ölçüldü (2026-07-28):
#   think:false            -> muhakeme düz metin olarak `response`'a sızdı
#   /no_think eki          -> etkisiz; muhakeme 200 token'ı yedi, response BOŞ
#   raw ChatML + prefill   -> ÇALIŞTI (7,9 sn, düşünme yok, gerçek cevap)
#
# Bu yüzden profil üç ayarı birlikte taşır; üçü tek tek işe yaramıyor:
#   raw_chatml     : Ollama şablonunu baypas et, ChatML'i biz kur.
#   think_prefill  : asistan sırasını boş <think></think> çiftiyle başlat ->
#                    model düşünme bölümünü KAPANMIŞ bulur, doğrudan yazar.
#   think_alani    : istek gövdesindeki "think" alanı (None -> alanı hiç koyma).
#   stop           : profil zorunlu stop dizisi. RAW modda `\n\n` KULLANILAMAZ --
#                    prefill'in kendi `\n\n`'i üretimi daha ilk anda keserdi.
VARSAYILAN_MODEL_PROFILI = {
    "raw_chatml": False,
    "think_prefill": False,
    "think_alani": False,   # mevcut davranış: "think": false gönder
    "stop": None,           # çağıranın verdiği stop kullanılır
}

# Anahtar: model adında ARANAN parça (küçük harf, ilk eşleşen kazanır).
MODEL_PROFILLERI: dict[str, dict] = {
    "trendyol": {
        "raw_chatml": True,
        "think_prefill": True,
        "think_alani": None,
        "stop": ["<|im_end|>"],
    },
}


# ---------------------------------------------------------------------------
# SAĞLAYICI KATMANI (2026-07-29) -- yerel Ollama yanında bulut (Groq) yolu.
#
# NEDEN: qwen3:8b pilotunda retry %31-40, kalan ihlal %16 ve kusurların türü
# (cümle ortasında kesik, bozuk dilbilgisi, "kategori adını kullanma"ya
# uymama) MODEL KAPASİTESİ eksenindeydi -- prompt yamasıyla çözülmüyordu
# (docs/arsiv/faz-b-prompt.md §15, "B yolu: daha güçlü modele taşı").
#
# TASARIM: mevcut `ollama_cagir` gövdesine DOKUNULMADI. Sağlayıcı modül
# düzeyinde bir kez ayarlanır (`saglayici_ayarla`), `ollama_cagir` başında
# tek satırla dallanır. Böylece tüm çağrı yerleri (pilot, VS, retry, runner)
# değişmeden çalışır.
#
# ÜCRETSİZ KATMAN KOTASI bağlayıcıdır, model hızı değil: 30 istek/dk,
# 30.000 token/dk. Saniyede 0,5 istek demek -- bu yüzden `--workers`
# yükseltmenin faydası yok ve istemci tarafında hız sınırlayıcı ŞART
# (yoksa 429 alır, o faturalar atlanır).
# ---------------------------------------------------------------------------

GROQ_HOST_VARSAYILAN = "https://api.groq.com/openai/v1"
# Kaggle/Colab notebook icinde ayaga kalkan vLLM sunucusu (OpenAI-uyumlu).
VLLM_HOST_VARSAYILAN = "http://localhost:8000/v1"

_SAGLAYICI = "ollama"
_GROQ_ANAHTAR: str | None = None
_HIZ_SINIRLAYICI = None


def env_yukle(yol: str = ".env") -> dict[str, str]:
    """Basit .env okuyucu -- python-dotenv bağımlılığı eklememek için.

    `AD=deger` satırlarını okur; boş satır ve `#` yorumlarını atlar. Değerin
    etrafındaki tırnakları soyar. Dosya yoksa boş dict döner."""
    degerler: dict[str, str] = {}
    p = Path(yol)
    if not p.exists():
        return degerler
    for satir in p.read_text(encoding="utf-8").splitlines():
        satir = satir.strip()
        if not satir or satir.startswith("#") or "=" not in satir:
            continue
        ad, _, deger = satir.partition("=")
        degerler[ad.strip()] = deger.strip().strip("'\"")
    return degerler


class _HizSinirlayici:
    """Thread-güvenli token kovası: dakikada en fazla `istek_dk` çağrı.

    ThreadPoolExecutor ile paralel çalışıldığı için kilit şart. Kotayı istemci
    tarafında uygulamak, sunucudan 429 yiyip faturayı kaybetmekten iyidir."""

    def __init__(self, istek_dk: int):
        self.aralik = 60.0 / max(istek_dk, 1)
        self._kilit = threading.Lock()
        self._sonraki = 0.0

    def bekle(self) -> None:
        with self._kilit:
            simdi = time.monotonic()
            bekleme = self._sonraki - simdi
            if bekleme > 0:
                time.sleep(bekleme)
                simdi = time.monotonic()
            self._sonraki = simdi + self.aralik


def saglayici_ayarla(ad: str, istek_dk: int = 30, env_yolu: str = ".env",
                     host: str | None = None) -> str:
    """Sağlayıcıyı ayarlar; kullanılacak varsayılan host'u döner.

    Desteklenen OpenAI-uyumlu yollar:
      'groq'  -> bulut; API anahtarı ZORUNLU (.env: GROQ_API_KEY)
      'vllm'  -> kendi sunucun (Kaggle/Colab notebook'unda vLLM); anahtar YOK,
                 kota da yok -> hız sınırlayıcı devre dışı, `host` verilmeli.

    Anahtar gerektiği hâlde yoksa AÇIK hata verir: sessizce Ollama'ya düşmek,
    saatler süren bir koşuyu yanlış modelle tamamlatırdı."""
    global _SAGLAYICI, _GROQ_ANAHTAR, _HIZ_SINIRLAYICI
    _SAGLAYICI = ad
    if ad == "ollama":
        return OLLAMA_HOST_VARSAYILAN
    if ad == "vllm":
        # Kendi sunucun: kota yok, sinirlayici yok, anahtar yok. `_SAGLAYICI`
        # 'groq' olmadigi icin _groq_cagir'in anahtar basligi bos gecer --
        # vLLM Authorization basligini yok sayar.
        _GROQ_ANAHTAR = os.environ.get("VLLM_API_KEY", "")
        _HIZ_SINIRLAYICI = None
        return host or VLLM_HOST_VARSAYILAN
    anahtar = os.environ.get("GROQ_API_KEY") or env_yukle(env_yolu).get("GROQ_API_KEY")
    if not anahtar:
        raise SystemExit(
            "GROQ_API_KEY bulunamadi. .env dosyasina su satiri ekleyin:\n"
            "  GROQ_API_KEY=gsk_..."
        )
    _GROQ_ANAHTAR = anahtar
    _HIZ_SINIRLAYICI = _HizSinirlayici(istek_dk)
    return host or GROQ_HOST_VARSAYILAN


def _groq_cagir(
    system_prompt: str, user_prompt: str, model: str, host: str,
    num_predict: int, temperature: float, seed: int | None,
    stop: list[str] | None, ham: bool, bilgi: dict | None,
    min_p: float = 0.1,
) -> str:
    """OpenAI-uyumlu sohbet tamamlama. Ollama parametrelerinin karşılıkları:

        num_predict -> max_tokens      | done_reason -> finish_reason
        system/prompt -> messages[]    | keep_alive/num_ctx -> yok (sunucu yönetir)

    `min_p`/`repeat_penalty` SAĞLAYICIYA GÖRE değişir (2026-07-29'da düzeltildi):

    - groq  -> gönderilmez; standart OpenAI şeması bu alanları tanımaz.
    - vllm  -> GÖNDERİLİR. vLLM OpenAI şemasını genişletir ve bu ikisini kabul
      eder (canlı doğrulandı, 200 döndü). Göndermezsek sunucu modelin
      `generation_config.json`'ındaki varsayılanı uygular; yani ayar bizde değil
      model dosyasında olur. Kaggle'daki ilk 32B koşusunda tam bu oldu:
      repetition penalty fiilen devre dışı kaldı ve `latin_disi`/`verbatim_kopya`
      ihlalleri çıktı. Değerler Ollama gövdesiyle BİREBİR aynı tutulur
      (min_p kategoriden gelir, repeat_penalty 1.15) -- yerel kalibrasyon korunsun.

    `top_k` KASTEN gönderilmez: Ollama yolu da göndermiyor, eklemek kalibrasyonu
    değiştirirdi."""
    govde = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": num_predict,
        "temperature": temperature,
        "top_p": 0.95,
        "stream": False,
    }
    if _SAGLAYICI == "vllm":
        govde["min_p"] = min_p
        # `repetition_penalty` GONDERILMIYOR -- ISIM AYNI, SEMANTIK FARKLI.
        # Ollama/llama.cpp `repeat_penalty`: son `repeat_last_n` (vars. 64) token.
        # vLLM `repetition_penalty`: PROMPT token'larini DA cezalandirir.
        # ~1000 token'lik Turkce prompt'u 1.15 ile cezalandirmak modeli tam da
        # kullanmasi gereken kelimelerden uzaklastirdi. Olculdu (Kaggle, 500'er
        # kayit): latin_disi 5->18, uzunluk ihlali 46->75, maks uzunluk 360->451.
        # min_p semantigi ise iki tarafta AYNI; o kaliyor.
        #
        # Qwen3 hibrit dusunen bir model ve vLLM'de dusunme VARSAYILAN ACIK.
        # Kapatilmazsa <think> blogu max_tokens'i yer, `kesik` ihlali patlar.
        # CLAUDE.md §5: yerel qwen3'te de `think` kapali; `/no_think` eki ETKISIZ.
        # 2507 'Instruct' surumleri zaten dusunmez ama alan zararsizdir.
        if "qwen3" in model.lower():
            govde["chat_template_kwargs"] = {"enable_thinking": False}
    if seed is not None:
        govde["seed"] = seed
    if stop:
        govde["stop"] = stop

    # Anahtar bossa (vllm) Authorization basligi HIC gonderilmez.
    basliklar = {"Authorization": f"Bearer {_GROQ_ANAHTAR}"} if _GROQ_ANAHTAR else {}
    # 429'da SABIRLI ol: kota penceresi 60 sn'lik, 5 denemede (üstel geri çekilme
    # ile ~31 sn) pencere kapanmadan pes ediliyordu ve fatura KAYBOLUYORDU
    # (32'lik pilotta 2 kayıp). Pencereyi rahat aşacak kadar dene.
    for deneme in range(8):
        if _HIZ_SINIRLAYICI is not None:
            _HIZ_SINIRLAYICI.bekle()
        # 90 -> 180: Kaggle'da 2xT4 uzerindeki 32B'de 16 worker'la tek istek 90 sn'yi
        # asabiliyor; asinca istisna firliyor ve FATURA DUSUYOR (batch_0001'de 500'un
        # 28'i = %5,6 kayip, 20k'da ~1.100 kayit ederdi). Yavas sunucuda beklemek
        # kaydi kaybetmekten ucuz.
        yanit = http_session.post(
            f"{host}/chat/completions", json=govde, headers=basliklar, timeout=180
        )
        if yanit.status_code == 429:
            # Sunucu Retry-After veriyorsa ONA uy (kotanın gerçek kalan süresi),
            # yoksa üstel geri çekil.
            bekle = float(yanit.headers.get("retry-after", min(2 ** deneme, 30)))
            time.sleep(min(bekle, 65))
            continue
        yanit.raise_for_status()
        break
    else:
        raise RuntimeError("Groq: 8 denemede de kota asimi (429) asilamadi")

    cevap = yanit.json()
    secim = cevap["choices"][0]
    if bilgi is not None:
        # 'kesik' ihlali bu alandan türüyor -- Ollama'daki done_reason'ın karşılığı.
        bilgi["done_reason"] = "length" if secim.get("finish_reason") == "length" else secim.get("finish_reason")
    metin = (secim["message"].get("content") or "")
    metin = _THINK_REGEX.sub("", metin).strip()
    return metin if ham else cikti_temizle(metin)


def model_profili(model: str) -> dict:
    ad = (model or "").lower()
    for anahtar, profil in MODEL_PROFILLERI.items():
        if anahtar in ad:
            return {**VARSAYILAN_MODEL_PROFILI, **profil}
    return dict(VARSAYILAN_MODEL_PROFILI)


def chatml_sar(system_prompt: str, user_prompt: str, think_prefill: bool) -> str:
    """system+user'ı ChatML'e sarar (raw mod). Sistem bölümü SABİT kaldığı için
    Ollama'nın prefix önbelleği raw modda da korunur."""
    p = (
        "<|im_start|>system\n" + system_prompt + "<|im_end|>\n"
        "<|im_start|>user\n" + user_prompt + "<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    return p + "<think>\n\n</think>\n\n" if think_prefill else p

POLICY_YASAKLI_KATEGORI_ADLARI = {"alkol", "eglence", "tutun_urunleri", "kumar"}
KATEGORILER = ("yeterli", "yetersiz", "manipulatif", "ai_uretimi")

# Model üretim yerine reddederse (güvenlik/moderasyon refleksi) yakalanır -> "red"
# ihlali olarak retry tetiklenir. Çıktı temizlemeden SONRA da hayatta kalan
# kalıplar (temizleme redleri silmez, çünkü red metni de "anlamlı" satırdır).
RED_KALIPLARI = ("yerine getiremem", "yardımcı olamam", "yardimci olamam",
                 "üretemem", "uretemem", "i can't", "i cannot", "as an ai")


def kalemler_ozetle(kalemler: list[dict]) -> str:
    return ", ".join(f"{k['aciklama']} ({k['harcama_kategorisi']})" for k in kalemler)


def kalemler_ozetle_prompt(kalemler: list[dict]) -> str:
    """Prompt için kalem özeti: kategori adı İNSAN-OKUR (alt çizgisiz) gösterilir.
    Böylece model ham enum'u ('kisisel_bakim') kopyalayamaz -> enum sızıntısı
    KAYNAĞINDA engellenir (retry'a kalmadan). kalemler_ozetle ise kayıt/MD için
    ham haliyle kalır."""
    return ", ".join(f"{k['aciklama']} ({k['harcama_kategorisi'].replace('_', ' ')})" for k in kalemler)


def yasakli_kalem_bul(kalemler: list[dict]) -> dict | None:
    for k in kalemler:
        if k["harcama_kategorisi"] in POLICY_YASAKLI_KATEGORI_ADLARI:
            return k
    return None


# Firma unvanindaki hukuki ek/suffix'leri temizler -- LLM'e "Yilmaz Gida San.
# ve Tic. Ltd. Şti." yerine "Yilmaz Gida" gibi konuşma diline yakin bir isim
# vermek icin (field_generator.py:IS_KOLU_SUFFIX ile SENKRON tutulmali).
# DİAKRİTİĞE DAYANIKLI yazılır (2026-07-30): burada eskiden literal `Danişmanlik`
# duruyordu ve field_generator'daki yazım `Danışmanlık`a düzeltildiğinde eşleşme
# SESSİZCE kesilecekti -- prompt'a 'X Danışmanlık A.Ş.' kısaltılmamış girerdi.
# Sabitler arası "senkron tut" notu bir insan sözüdür, regex artık kendini korur.
_UNVAN_EKLERI_REGEX = (
    r"\b(A\.Ş\.|Ltd\.\s*Şti\.|Tic\.|San\.|ve|Paz\.|Turizm|Nak\.|Otelcilik"
    r"|Dan[iı]şmanl[iı]k|Prodüksiyon|Konfeksiyon|Kozmetik|Global|İç|Diş"
    r"|Ticaret|Sanayi|Taş\.)(?=\s|$)"
)

# Kısaltma YALNIZCA SENTETİK adlara uygulanır. Regex sektör kelimelerini de
# ("Turizm", "Kozmetik", "Ticaret", "ve") söktüğü için registry'deki GERÇEK OSM
# adlarını bozuyordu: 'Kozmetik Shop' -> 'Shop', 'Umut Market ve Şarküteri' ->
# 'Umut Market Şarküteri', 'Cansur İç Giyim' -> 'Cansur Giyim' (18.623 OSM adının
# 414'ü, %2,2). Ayrım için registry lookup'a gerek yok -- SONDAKİ hukuki ek
# kusursuz bir proxy (ölçüldü): sentetik 5896/5896 eşleşiyor, osm 23/18623 (%0,12),
# şahıs 0/4363. Eşleşmeyen ad (OSM gerçeği / şahıs şirketi) OLDUĞU GİBİ döner.
_SENTETIK_SON_EK = re.compile(r"(A\.Ş\.|Ltd\.\s*Şti\.|San\.|Tic\.|Nak\.|Paz\.|Taş\.)\s*$")


def firma_adi_kisalt(unvan: str) -> str:
    if not _SENTETIK_SON_EK.search(unvan):
        return unvan
    kisa = re.sub(_UNVAN_EKLERI_REGEX, "", unvan)
    kisa = re.sub(r"\s+", " ", kisa).strip(" .,-")
    return kisa if kisa else unvan


# Ürün adının sonunda ölçü/ambalaj ya da renk bilgisi kalabilir ('... 500 Yaprak',
# '... Siyah'); baş ismi bulmadan önce bunlar atılır.
_OLCU_AMBALAJ_KELIMELERI = {
    "yaprak", "adet", "adetli", "paket", "pkt", "kutu", "koli", "rulo", "sise",
    "şişe", "gram", "gr", "kilo", "kg", "litre", "lt", "ml", "cl", "metre", "mt",
}
_RENK_KELIMELERI = {
    "siyah", "beyaz", "kirmizi", "kırmızı", "mavi", "yesil", "yeşil", "gri", "sari",
    "sarı", "lacivert", "pembe", "mor", "kahverengi", "turuncu", "bej", "krem",
}

# LİSANS ŞARTI kuyruğu: 'Bitdefender GravityZone Business Security - 6 Kullanıcı
# - 3 Yıl' gibi adlarda baş isim BAŞTADIR, sondaki kısım lisans şartıdır. Türkçe
# "baş isim sonda" kuralı bu adlarda geçerli DEĞİL; sondan iki kelime alınınca
# 'kullanıcı yıl' çıkıyordu (ölçüldü: 4000 faturada 486 kalem böyle bozuluyor,
# 316'sı tam olarak 'kullanıcı yıl'). Bu kelimeler sondan atılır; hepsi
# atıldığında geriye anlamlı bir baş isim kalmazsa BAŞTAN alınır.
# KOKU / VARYANT kuyruğu -- market tipi adlarda ürün adından SONRA gelir ve
# Türkçenin "baş isim sonda" kuralını bozar:
#   'H.Sakir Guzl Sab.5Li ÇİÇEK BUKETİ 375Gr' -> baş isim 'Sab(un)', kuyruk koku
#   'Elidor 700 Ml Şampuan ONARICI BAKIM'     -> baş isim 'Şampuan', kuyruk varyant
#   'Fairy Sivi Bulasik Deterjani 480 Ml LİMON'
# Ölçüldü (30k fatura, son kelime frekansı): limon 911, lavanta 431, portakal 392,
# garantili 555. Bu kuyruk atılmazsa sadeleştirme KOKUYU ürün sanıyor ve o hatalı
# ad `yeterli` talimatına ÖRNEK olarak giriyordu -- pilottaki "çiçek aldım"
# çıktısının doğrudan kaynağı buydu.
#
# Meyve/çiçek adları yalnız SONDA ve BAŞKA kelime varken atılır; tek kelimelik
# 'Limon' (gerçekten limon alımı) korunur.
_KOKU_VARYANT_KELIMELERI = {
    # koku (meyve/çiçek/doğa)
    "limon", "lavanta", "portakal", "elma", "çiçek", "cicek", "buketi", "buket",
    "gül", "gul", "vanilya", "şeftali", "seftali", "nar", "karpuz", "çilek",
    "cilek", "okyanus", "orman", "bahar", "yasemin", "papatya", "narenciye",
    # varyant / niteleme
    "kokulu", "koku", "onarici", "onarıcı", "parlaklik", "parlaklık", "ferahlik",
    "ferahlık", "temel", "klasik", "classic", "sensitive", "ultra", "maxi",
    "extra", "garantili", "orjinal", "orijinal", "avantajli", "avantajlı",
    # 'bakim/bakım' SONDA neredeyse her zaman varyanttır ('Şampuan Onarıcı
    # BAKIM'); baş isim olduğu adlarda ('Cilt Bakım KREMİ') zaten son kelime
    # değildir, o yüzden atmak güvenli.
    "bakim", "bakım",
}

# Market/POS adlarındaki kısaltmalar. Sadeleştirme bunları açmazsa model
# 'guzl sab' gibi anlamsız bir ifade görüyor.
_KISALTMA_ACILIMI = {
    "sab": "sabun", "samp": "şampuan", "sampuan": "şampuan", "det": "deterjan",
    "deterjani": "deterjan", "bulasik": "bulaşık", "camasir": "çamaşır",
    "sivi": "sıvı", "yag": "yağ", "tem": "temizlik", "kag": "kağıt",
    "pesc": "peçete", "guzl": "", "kutulu": "", "paketi": "",
}

_LISANS_SARTI_KELIMELERI = {
    "yil", "yıl", "yillik", "yıllık", "ay", "aylik", "aylık", "kullanici",
    "kullanıcı", "cihaz", "server", "sunucu", "mobil", "pc", "bilgisayar",
    "lisans", "lisansi", "lisansı", "kisi", "kişi", "user",
    # AYNI SINIFTAN İKİNCİ KUYRUK (2026-07-29, ikinci pilot): paket ürünler
    # '... + Google Play Hediye Kodu 100 TL' ile bitiyor -> sondan iki kelime
    # 'kodu tl' veriyordu (ölçüldü: 2374 kalem, 1945'i tam olarak bu).
    # Pilotta "Kodu tl aldım, müşteri sunumu için." diye görünüyordu.
    "tl", "try", "kodu", "kod", "hediye", "bonus", "cek", "çek", "kupon",
    # ölçü kuyrukları ('10x40 cm', '96x144x30 cm')
    "cm", "mm", "mt", "inc", "inç", "li", "lu", "lı", "lü",
}

# ÜÇÜNCÜ KUYRUK SINIFI (2026-07-30, model karşılaştırma pilotu): SKU / MODEL KODU.
# Ölçüldü (200 fiş / 514 kalem): 10 kalem (%1,95) bozuk sadeleştirme veriyordu --
# '4byz monza', '120g servis', 'sunger 1ad', 'kahve 3x250'. Pilotta "Müşteri
# ziyareti için 4byz monza aldım." diye görünüyordu.
#
# Kelime LİSTESİYLE çözülemez (kodlar sonsuz varyasyonda) ve KUYRUK DÖNGÜSÜYLE de
# çözülemez, çünkü kod SONDA olmak zorunda değil: '... Eames Sandalye Snd3016-4byz
# Monza' adında kodlar ORTADA, son kelime ('monza') gerçek bir kelime olduğu için
# kuyruk döngüsü hiç başlamıyor. O yüzden desen bazlı ve TÜM konumlardan silinir.
#
# İSTİSNA -- KISA MODEL BELİRTECİ KORUNUR ('v15', 'g2', 'a4'): bunlar insanın da
# söylediği ayırt edici adlardır ('lenovo v15' iyi bir sadeleştirmedir). Ölçüt
# harf-önce + kısa; SKU'lar rakam-önce ('4byz', '120g', '1ad') ya da uzun-karışık
# ('snd3016', '21mhakkh2318pnt0') olur.
_MODEL_BELIRTECI_KORU = re.compile(r"^[^\W\d_]{1,2}\d{1,4}$", re.UNICODE)
_SKU_KODU = re.compile(r"^(?=\w*\d)(?=\w*[^\W\d_])\w+$|^\d+[xX]\d+$", re.UNICODE)

# NİTELİK KUYRUĞU -- `_LISANS_SARTI_KELIMELERI`'nden AYRI TUTULMALI. Fark KONUM
# VARSAYIMINDADIR: lisans kuyruğu atıldığında baş isim BAŞA kayar ('Bitdefender
# GravityZone Business Security - 6 Kullanıcı - 3 Yıl' -> 'bitdefender gravityzone'),
# ama nitelik kuyruğu atıldığında Türkçe kuralı GEÇERLİ KALIR, baş isim hâlâ SONDADIR
# ('Chia Tohumu (glutensiz) 1 Kg' -> 'chia tohumu'). İkisi aynı kümeye konursa
# parantezli nitelik baştan-alma dalını tetikler ve 'the spicex' çıkar.
_NITELIK_KUYRUGU = {
    "glutensiz", "sekersiz", "şekersiz", "laktozsuz", "katkisiz", "katkısız",
    "tuzsuz", "organik", "vegan", "rafine", "servis", "porsiyon",
}

# SONDAKI NITELEYICI -- atilmaz, BAS ISMIN ONUNE alinir (2026-08-01).
#
# 'son iki anlamli kelime' kurali Turkcede bas ismin SONDA oldugu varsayimina
# dayanir. Ad SIFATLA bitince varsayim coker (olculdu: 4.000 urunde %13,8):
#     'Icim Sut Cikolatali 200Ml'    -> 'sut cikolatali'   (ters tamlama)
#     'F Neffis Sut Yarim Yagli 1Lt' -> 'yarim yagli'      (bas isim DUSTU)
#     'Papia Cep Mendili Cocuk'      -> 'mendili cocuk'    (anlamsiz)
# Model bunlari sadakatle cumleye yerlestirdigi icin 'sacma aciklama'nin
# dogrudan kaynagiydi.
#
# LISTE KURATORLU, SON EK KURALI DEGIL -- KASITLI. '-li/-lu' son ekiyle sifat
# yakalamak cazip ama 'mendili', 'peyniri', 'ekmegi' de oyle biter (iyelik eki)
# ve onlar BAS ISIMDIR; sezgisel kural tam da bu fonksiyonun gecmisteki
# regresyon kaynagi. Liste veriden cikarildi (havuzda son kelime frekansi).
#
# GUVENLIK GARANTISI: son kelime bu kumede DEGILSE davranis bit bit eskisiyle
# ayni kalir. Yani duzeltme yalnizca bozuk vakalara dokunur.
# Her uye HAVUZDA fiilen son kelime olarak GECIYOR (frekansla dogrulandi).
# Hic gecmeyen adaylar (bebek, haslanmis, kizarmis, kuru, sebzeli, tavuklu,
# kiymali) KASITLI olarak YOK: fayda getirmeden risk tasirlar.
#
# 'kavrulmus' DENENDI ve GERI ALINDI: 'Petibor Cifte Kavrulmus' -> eski cikti
# 'cifte kavrulmus' DOGRUYDU, yeniden siralama onu 'kavrulmus cifte' yapip
# kaliplasmis tamlamayi bozdu. Sifat gibi gorunen SIFAT-FIIL'ler (-mis) bu
# listeye GIRMEZ; kalibin parcasi olabilirler.
_SON_NITELEYICI = {
    # tat / icerik
    "kakaolu", "cikolatali", "çikolatalı", "findikli", "fındıklı", "kremali",
    "kremalı", "meyveli", "sutlu", "sütlü", "yagli", "yağlı", "tuzlu",
    "baharatli", "baharatlı", "dolgulu", "sekerli", "şekerli", "cilekli",
    "çilekli", "muzlu", "kayisili", "kayısılı", "visneli", "vişneli",
    "portakalli", "portakallı", "limonlu", "naneli", "bademli", "cevizli",
    "fistikli", "fıstıklı", "susamli", "susamlı", "peynirli", "mantarli",
    "mantarlı",
    # hazirlanis / nitelik
    "sade", "taze", "dilimli", "sikmalik", "sıkmalık", "kiymalik", "kıymalık",
    "yarim", "yarım", "klasik", "normal", "light",
    # boyut / hedef kitle / paket sinifi
    "orta", "buyuk", "büyük", "kucuk", "küçük", "mini", "eko", "ekonomik",
    "ekstra", "ozel", "özel", "cocuk", "çocuk",
}

# Baglaclar bas isim ya da niteleyici DEGILDIR; birakilirsa 'Yasemin Ve Aloera'
# -> 've aloera' gibi baglacla BASLAYAN sonuc cikiyordu (havuzun %0,5'i).
_BAGLAC_KELIMELERI = {"ve", "ile", "veya", "ya", "and", "&"}


def _niteleyici_onune_al(kelimeler: list[str]) -> list[str]:
    """Son kelime niteleyiciyse onu bas ismin ONUNE alip iki kelime dondurur.

    Bas isim = SONDAN geriye dogru ilk NITELEYICI OLMAYAN kelime. Boylece bas
    isim iki niteleyici arkasinda kalsa da kurtarilir ('Sut Yarim Yagli').
    Hicbir niteleyici yoksa ya da hepsi niteleyiciyse liste AYNEN doner ->
    cagiran taraf eski davranisi (son iki kelime) uygular."""
    if len(kelimeler) < 2 or _tr_normalize(kelimeler[-1]) not in _SON_NITELEYICI:
        return kelimeler
    for i in range(len(kelimeler) - 1, -1, -1):
        if _tr_normalize(kelimeler[i]) not in _SON_NITELEYICI:
            return [kelimeler[-1], kelimeler[i]]      # 'cikolatali' + 'sut'
    return kelimeler


def kalem_adi_sadelestir(ad: str) -> str:
    """Ham kalem adını çalışanın yazacağı hâle indirger.

    Türkçede isim tamlamasının BAŞ İSMİ SONDADIR ('F Saff Sıvı El Sabunu 500Ml' ->
    'el sabunu', 'Renkli A4 Fotokopi Kağıdı 500 Yaprak' -> 'fotokopi kağıdı'), bu
    yüzden ölçü/ambalaj/renk gürültüsü atıldıktan sonra SON iki anlamlı kelime alınır
    (baştan almak 'saff sıvı el' gibi bozuk sonuç veriyordu).

    Ölçü/model gürültüsü için mevcut `_URUN_DETAY_GURULTU` regex'i yeniden kullanılır
    (modülün alt kısmında tanımlı -- çalışma anında çözülür).

    `yeterli` talimatındaki SADELEŞTİRME örneğini faturanın KENDİ kaleminden kurmak
    için: sabit örnekler ('F Saff Sıvı El Sabunu 500Ml' -> 'el sabunu') 8B'de
    içeriğe sızıyordu (model örnekteki ürünü alakasız fişte anlatıyordu)."""
    # NFC normalize ŞART: 'Li̇mon' gibi adlarda 'i' + U+0307 (birleşen nokta)
    # ayrı kod noktası olarak duruyor; `[^\w\s]` onu boşluğa çevirip kelimeyi
    # ORTADAN bölüyordu ('Limon Kokulu' -> 'mon kokulu'). Ölçüldü: 20k faturada
    # kalemlerin %2,3'ü birleşen nokta içeriyor.
    # NFC tek başına YETMEZ: 'i' + U+0307 için önceden birleştirilmiş karakter
    # yoktur, o yüzden birleşen nokta ayrıca SİLİNİR (_tr_normalize'daki ile
    # aynı yöntem). Aksi halde `[^\w\s]` onu boşluğa çevirip kelimeyi ortadan
    # bölüyor: 'Li̇mon Kokulu' -> 'li mon'.
    sade = unicodedata.normalize("NFC", ad).replace("̇", "")
    sade = _URUN_DETAY_GURULTU.sub(" ", sade)
    sade = re.sub(r"[^\w\s]", " ", sade)
    kelimeler = [k for k in sade.split()
                 if len(k) > 1 and not k.isdigit()
                 and _tr_normalize(k) not in _BAGLAC_KELIMELERI]
    while kelimeler and (_tr_normalize(kelimeler[-1]) in _OLCU_AMBALAJ_KELIMELERI
                         or _tr_normalize(kelimeler[-1]) in _RENK_KELIMELERI):
        kelimeler.pop()

    # KOKU/VARYANT kuyruğunu at (bkz. _KOKU_VARYANT_KELIMELERI). Tek kelime
    # kalırsa DURMA: 'Limon' tek başına gerçekten limon alımıdır, koku değil.
    while len(kelimeler) > 1 and _tr_normalize(kelimeler[-1]) in _KOKU_VARYANT_KELIMELERI:
        kelimeler.pop()

    # Kısaltmaları aç ('sab' -> 'sabun'); karşılığı boş olanlar ('guzl') düşer.
    acilmis: list[str] = []
    for k in kelimeler:
        yeni = _KISALTMA_ACILIMI.get(_tr_normalize(k), k)
        if yeni:
            acilmis.append(yeni)
    kelimeler = acilmis or kelimeler

    # SKU/model kodlarını TÜM konumlardan sil (kuyrukta olmak zorunda değil, bkz.
    # _SKU_KODU). Hepsi koddan ibaretse dokunulmaz -- geriye hiç kelime bırakmamak
    # yerine bozuk adı olduğu gibi bırakmak yeğdir.
    kodsuz = [k for k in kelimeler
              if not _SKU_KODU.match(k) or _MODEL_BELIRTECI_KORU.match(k)]
    if kodsuz:
        kelimeler = kodsuz

    # Kod silinince geriye AYNI kelime iki kez kalabiliyor ('ACER Aspire3
    # A315-56-327t ... Nx Hs5ey Nx ...' -> 'nx nx'). Marka/model adı ham adda
    # zaten tekrar ediyordu, araya giren kod onu gizliyordu. Ardışık tekrarı
    # düşür -- 'nx nx' insan metninde gülünç durur.
    tekrarsiz: list[str] = []
    for k in kelimeler:
        if not tekrarsiz or _tr_normalize(k) != _tr_normalize(tekrarsiz[-1]):
            tekrarsiz.append(k)
    kelimeler = tekrarsiz

    # Nitelik kuyruğunu at -- KONUM VARSAYIMINI DEĞİŞTİRMEZ, o yüzden aşağıdaki
    # lisans bloğundan ÖNCE ve ondan AYRI uygulanır (bkz. _NITELIK_KUYRUGU).
    while len(kelimeler) > 1 and _tr_normalize(kelimeler[-1]) in _NITELIK_KUYRUGU:
        kelimeler.pop()

    # Lisans şartı kuyruğunu at, ama SİL-BAŞTAN ALMA kararı için ayrı tut:
    # kuyruk atıldıktan sonra geriye kelime kalmıyorsa ad tamamen şarttan
    # ibaretti demektir, o zaman baştan almak tek doğru seçenek.
    govde = list(kelimeler)
    while govde and _tr_normalize(govde[-1]) in _LISANS_SARTI_KELIMELERI:
        govde.pop()
    if govde:
        # Kuyruk gerçekten atıldıysa baş isim BAŞTA demektir (lisans adı);
        # aksi halde Türkçe kuralı geçerli, sondan al.
        if len(govde) < len(kelimeler):
            return " ".join(govde[:2]).lower()
        # TURKCE dali: son kelime niteleyiciyse bas ismin onune alinir.
        # Niteleyici degilse `_niteleyici_onune_al` listeyi AYNEN dondurur ->
        # eski davranis (son iki kelime) korunur.
        return " ".join(_niteleyici_onune_al(govde)[-2:]).lower()
    return " ".join(kelimeler[:2]).lower()


def aykiri_kalem_bul(kalemler: list[dict]) -> dict | None:
    if len(kalemler) < 2:
        return None
    kategori_sayaci = Counter(k["harcama_kategorisi"] for k in kalemler)
    baskin_kategori, baskin_adet = kategori_sayaci.most_common(1)[0]
    azinlik_kategorileri = {kat for kat, adet in kategori_sayaci.items() if adet < baskin_adet}
    if not azinlik_kategorileri:
        return None
    for k in kalemler:
        if k["harcama_kategorisi"] in azinlik_kategorileri:
            return k
    return None


def gizlenecek_kalem_bul(kalemler: list[dict]) -> dict | None:
    return yasakli_kalem_bul(kalemler) or aykiri_kalem_bul(kalemler)


# ---------------------------------------------------------------------------
# UZUNLUK HEDEFİ (kategoriden BAĞIMSIZ, yumuşak ağırlıklı): araştırma, 8B gibi
# küçük modellerde çeşitliliğin EN güçlü kaldıracının kategoriden bağımsız uzunluk
# varyasyonu olduğunu gösteriyor -- ince persona detayını açık ara geçiyor (bkz.
# arXiv:2505.17390). Persona ekseni 8B'de neredeyse etkisiz olduğu için kaldırıldı,
# yerine bu geldi. Ağırlıklar YUMUŞAK: her kategori her uzunluğu alabilir (leakage
# yok -> uzunluk temiz bir kategori ayracı olamaz), ama semantik makullük korunur
# (ai_uretimi doğal olarak uzuna, yetersiz kısaya eğilir).
# Her hedef: (ad, prompt_tarifi, alt_karakter, ust_karakter).
# ---------------------------------------------------------------------------

# Üst sınırlar sözel tarifle uyumlu olacak şekilde gevşetildi: iki doğal Türkçe
# cümle rahatça 150 karakteri, üç cümle 250'yi buluyor; eski bantlar (135/230)
# gereksiz `uzunluk` ihlali -> boşuna retry üretiyordu. Nihai değerler pilot
# ölçümüyle (aciklama_analiz.py, kalan-ihlal frekansı) kalibre edilir.
UZUNLUK_HEDEFLERI = [
    ("çok kısa", "en fazla 4-5 kelimelik tek bir öbek", 8, 45),
    ("kısa", "tek cümle, 6-10 kelime", 20, 85),
    ("orta", "1-2 cümle", 45, 150),
    # ÜST SINIR 250 -> 200 (2026-07-29). Ölçüldü: manipulatif dağılmalarının
    # TAMAMI uzun bantta -- 244 krk'lık bir çıktıda İngilizce kelime bile sızdı
    # ('ortamınınpositive bir atmosferde'), 148-153 krk olanlar geveze, 34-83
    # krk olanlar temiz. 250 karakterlik alan modele dağılacak yer bırakıyordu.
    #
    # KATEGORİYE ÖZEL kısaltma YAPILMADI (manipulatif'e ayrı tavan gibi):
    # `uzunluk_hedefi_sec` kasten kategoriden bağımsızdır, amacı kategori↔uzunluk
    # sahte korelasyonunu kırmaktır (leakage önlemi). Bandı GLOBAL indirmek o
    # bağımsızlığı korur.
    #
    # KESİLME RİSKİ YOK: `token_limiti = ust/2.0 + 30` -> 200'de 130 token, Llama
    # Türkçede 3,24 krk/token ürettiği için ~420 karakterlik fiili kapasite =
    # hedefin 2,1 katı. Eski kesilme sorunu qwen3'teydi (~2 krk/token, bütçe dar).
    ("uzun", "2-3 cümle, biraz daha detaylı", 90, 200),
]

# Kategori başına yumuşak ağırlık: dört uzunluğa da şans verir, sadece eğim farkı.
_UZUNLUK_AGIRLIK = {
    "yetersiz": [0.40, 0.35, 0.20, 0.05],
    # ai_uretimi: HEM kısa (kalıba uyan tek öbek) HEM ölçülü-uzun (1-2 cümle resmi
    # paragraf) alabilsin -> 'uzunsa ai' diye TEK YÖNLÜ sahte sinyal vermeyelim
    # (ai kısa da olabilir, uzun da). Aşırı uzun (3+ cümle/paragraf) hariç, dört
    # uzunluğa da makul şans. Karakteri artık system prompt taşıyor.
    # "çok kısa" (8-45 krk) ai_uretimi için İMKANSIZ: zorunlu açılış (~25 krk) +
    # kapanış (~30 krk) zaten ~70 karakter eder -> ihlal GARANTİ, retry düzeltemez.
    # Pilotta 4 uzunluk ihlalinin 3'ü buydu ve ai retry oranını %50'ye çıkarmıştı.
    "ai_uretimi": [0.00, 0.30, 0.40, 0.30],
    # manipulatif de "çok kısa" alamaz -- kurumsal kılıf 45 karaktere sığmıyor (bkz.
    # ihlalleri_bul'daki 55 karakterlik alt sınır).
    "manipulatif": [0.00, 0.30, 0.45, 0.25],
    # yeterli de "çok kısa" (8-45 krk) ALAMAZ (2026-07-30) -- manipulatif ve
    # ai_uretimi için zaten kapalı olan durumun aynısı, ölçümle doğrulandı:
    #     "çok kısa" ATANMA oranı %20  ↔  fiilen ≤45 krk YAZILAN %7
    # Aradaki ~%13 KARŞILANAMAZ hedef: `yeterli` amacı + kalemi + (yeni kuralla)
    # fişten türetilemeyen bir sebebi 45 karaktere sığdıramıyor. Sonuç garanti
    # ihlal ve retry'nin düzeltemediği bir flag.
    # KANIT: son pilotta 18 uzunluk ihlalinin 16'sı (%89) `yeterli`den geldi;
    # ihlallerin TAMAMI alt sınır ihlaliydi (hiçbir metin üst toleransı aşmadı).
    "yeterli": [0.00, 0.40, 0.40, 0.20],
}


def uzunluk_hedefi_sec(kategori: str) -> tuple[str, str, int, int]:
    """Kategoriden bağımsız (yumuşak ağırlıklı) bir uzunluk hedefi seçer.
    Dönüş: (ad, prompt_tarifi, alt_karakter, ust_karakter)."""
    agirlik = _UZUNLUK_AGIRLIK.get(kategori, [0.25, 0.25, 0.25, 0.25])
    return random.choices(UZUNLUK_HEDEFLERI, weights=agirlik, k=1)[0]


# ---------------------------------------------------------------------------
# FEW-SHOT: kategori başına küratörlü örnekler. Model tarif yerine TAKLİT eder.
# Çekirdeğin pozitif-çerçeveleme felsefesine uyumlu: yeterli örnekleri aktif fiil
# (aldım/ödedim) taşır -- yasak kalıp GÖSTERİLMEZ (pembe-fil etkisi). ai_uretimi
# örnekleri AI_URETIMI_KAPANIS_IPUCLARI havuzuyla tutarlı kapanışlar kullanır.
# ---------------------------------------------------------------------------

FEWSHOT = {
    # yeterli örnekleri YETERLI_ISKELE yapılarını örnekler: amaç-önce / kalem-önce /
    # olay-çıpalı / iki-parçalı; hepsi Türkçenin neden-sonuç, amaç-sonuç bağlacını taşır.
    "yeterli": [
        "Bölge bayi ziyaretinde ekiple öğle yemeği için ödedim.",
        "Yeni ekip üyeleri için kırtasiye ve toner aldım, stok bitmişti.",
        "Müşteri sunumu öncesi toplantı odasına ikram ısmarladım.",
        "Saha kurulumunda kullanmak üzere kablo ve bağlantı parçası satın aldım.",
        "Toner bittiği için yenisini aldım, ay sonu raporlarını basmamız gerekiyordu.",
        "Fuar standında görevliydik, o yüzden ekibe öğle yemeği ısmarladım.",
        "Yeni gelen stajyerlere kulaklık aldım; açık ofiste toplantılara giremiyorlardı.",
    ],
    "yetersiz": [
        "genel ofis ihtiyacı",
        "iş gideri",
        "muhtelif harcama, departman için",
        "gerekliydi aldım",
    ],
    # ai_uretimi örnekleri BİLEREK farklı açılışlarla (İlgili/Söz konusu/İşbu/Fişe konu/
    # Bahse konu) -> model tek bir açılışa ('Belirtilen fiş...') çakılmasın.
    "ai_uretimi": [
        "İlgili masraf kalemi, kurumsal faaliyetlerin sürdürülebilirliği doğrultusunda gerçekleştirilmiştir.",
        "Söz konusu harcama, departmanın operasyonel gereksinimleri kapsamında temin edilmiştir.",
        "İşbu gider, ilgili birimin ihtiyaçları çerçevesinde değerlendirilmiştir.",
        "Fişe konu kalemler, kurumsal süreçlerin devamlılığı amacıyla tanzim edilmiştir.",
        "Bahse konu masraf, mevzuat ve şirket politikaları doğrultusunda karşılanmıştır.",
    ],
}

# manipulatif few-shot'ı DALA GÖRE ayrılır. Tek ortak havuz kullanılırken kurnaz dal
# "abartılı vurgu KULLANMA" derken prompt'una vurgulu örnekler giriyordu -> dal ayrımı
# bulanıklaşıyordu. Her dal kendi hedef üslubunu görür.
FEWSHOT_MANIPULATIF = {
    # Gerçek/aykırı kalemi hiç anmadan kurumsal kılıf: sızıntı YOK, kılıf iddialı.
    "gizleme": [
        "Yönetim kurulu değerlendirme toplantısı kapsamında ağırlama gideri.",
        "Önemli müşteri ağırlama organizasyonu, tümüyle iş geliştirme amaçlı.",
        "Bölge bayileriyle yıl sonu değerlendirme programının temsil gideri.",
        "Stratejik iş ortağımızla yürütülen görüşmelerin ağırlama masrafı.",
        "Ekip motivasyon programı kapsamındaki kurumsal etkinlik gideri.",
    ],
    # Abartılı, savunmacı, ısrarcı: zorunlu vurgu bu dalda gelir.
    "bariz": [
        "Kesinlikle tamamen proje bütçesi kapsamındadır, iş dışı hiçbir kalem yoktur.",
        "Yüzde yüz iş amaçlı bir harcama, kişisel hiçbir yanı yok, emin olabilirsiniz.",
        "Bunu tümüyle iş için aldım, gerekirse toplantı kaydını da paylaşabilirim.",
        "Hiç şüphesiz şirket gideri; zaten başka türlü işi yürütmemiz mümkün değildi.",
        "Baştan sona iş kapsamında, gerçekten gerekliydi, lütfen onaylayın.",
    ],
    # Sakin, rutin görünen, sorgusuz onaylanmayı hedefleyen şişirilmiş kurumsal kılıf.
    "kurnaz": [
        "Departman değerlendirme toplantısı kapsamında rutin temsil gideri.",
        "Aylık operasyon planlaması görüşmesinin standart ağırlama kalemi.",
        "İş geliştirme sürecinin olağan gideri, bütçe kaleminde tanımlı.",
        "Saha ekibiyle yapılan periyodik koordinasyon toplantısının gideri.",
        "Yıllık tedarik planı çerçevesinde alınan mutat departman ihtiyacı.",
    ],
    # Tutarı hiç anmadan kaçınılmazlık kurar (limit_asimi dalı).
    "zorunluluk": [
        "Toplantı uzayınca açık olan tek yer orasıydı, mecburen oradan aldık.",
        "Acil ihtiyaçtı, başka tedarikçi bulamadım, işi durdurmamak için oradan aldım.",
        "Kurulum aynı gün bitmeliydi, elimizdeki tek seçenek buydu.",
        "Müşteri programı değişti, son anda oradan almak zorunda kaldım.",
        "Stok tükenmişti ve iş bekleyemezdi, bulabildiğim yerden temin ettim.",
    ],
    # MAĞDURİYET (2026-07-30) -- dal başına AYRI havuz ŞART (§6): ortak havuz
    # kullanılırsa bu dala vurgulu/kurumsal kılıflı örnekler girer ve karakter
    # bozulur. `zorunluluk`tan farkı öznede: orada İŞ dayatıyor, burada ÇALIŞANIN
    # başına geliyor. Örneklerde vurgu ifadesi YOK, şikâyet tonu YOK -- sakin
    # anlatım, karşı taraf hak versin diye.
    "magduriyet": [
        "Müşteri toplantısına yetişmem gerekiyordu, taksiye binmek zorunda kaldım.",
        "Görüşme uzayınca son servisi kaçırdım, geceyi orada geçirdim.",
        "Sahada beklemediğim bir aksilik çıktı, cebimden karşılamak durumunda kaldım.",
        "Ekip için hazırlık yapacaktık ama ofiste hiçbir şey yoktu, ben tamamladım.",
        "Dönüş yolunda araç bozuldu, orada beklerken bunu almak zorunda kaldım.",
    ],
}


def _fewshot_havuz(kategori: str, dal: str | None = None) -> list[str]:
    """Kategori (ve manipulatif ise DAL) için few-shot örnek havuzu."""
    if kategori == "manipulatif":
        if dal in FEWSHOT_MANIPULATIF:
            return FEWSHOT_MANIPULATIF[dal]
        return [o for havuz in FEWSHOT_MANIPULATIF.values() for o in havuz]
    return FEWSHOT.get(kategori, [])


# --- DIZGI CARPITMA: tipografik leakage panzehiri (2026-08-01) --------------
#
# OLCULDU (25k uretim + 100'luk Kaggle pilotu, ayni sonuc): kategori, metnin
# ICERIGINE HIC BAKILMADAN ayirt edilebiliyordu --
#     yeterli %100 nokta / yetersiz %0 nokta / ai_uretimi %100 nokta+buyuk harf
# Yalnizca "nokta var mi + buyuk harf mi" ile `yetersiz` %95 recall / %84
# precision ile bulunuyor. Bu, `uzunluk_hedefi_sec`in kategori-uzunluk
# korelasyonunu kirma gerekcesiyle AYNI sinif leakage.
#
# KAYNAK prompt'taki orneklerin DIZGISI: FEWSHOT[yeterli/ai/manipulatif]
# %100 buyuk harf + %100 nokta, YETERSIZ_ORNEK_HAVUZ (61 girdi) %0 + %0.
# Model uslubu degil DIZGIYI taklit ediyor.
#
# COZUM havuzun 90 literalini elle duzenlemek DEGIL (kirilgan, gozden kacar):
# ornekler prompt'a girerken dizgi OLASILIKLA cevriliyor. Havuz tek kaynak
# olarak kalir, yeni kural/token eklenmez (bkz. faz-b-prompt.md 15).
#
# AMAC %50/50 DEGIL: usengec calisanin kucuk harf yazmasi GERCEKCI ve
# korunmali. Amac kurali EGILIME cevirmek -- %100/%0 yerine ~%70/%30.
_DIZGI_CEVIRME_ORANI = 0.30

# OLCULDU (Kaggle pilotu, 100 kayit): duz %30 ile `yetersiz` nokta orani
# %0 -> %8 oldu, hedef %25-30'du. Kategorinin butun cercevesi ("bastan savma,
# usengec") modeli kucuk harfe itmeye devam ediyor; tek ornegi cevirmek
# yetmiyor. Ileride yukseltilecekse KATEGORIYE OZEL yapilmali ve `ai_uretimi`
# DUSUK birakilmali: onun %100 resmi dizgisi ARTEFAKT DEGIL kategorinin
# TANIMIDIR (urun_detay_kopya/tutar_sizinti'nin ai'de serbest olmasiyla ayni
# mantik -- AI AYRACI, sizinti degil). Duz orani yukseltmek onu da bozardi.


def _tr_bas_harf(harf: str, buyut: bool) -> str:
    """Turkce'ye UYGUN bas harf donusumu.

    Python'un varsayilani Turkce'de YANLIS: 'i'.upper() -> 'I' (dogrusu 'İ'),
    'İ'.lower() -> 'i' + U+0307 birlesen nokta (dogrusu 'i'). Ikincisi bu kod
    tabaninda zaten iz birakmis bir tuzak (bkz. kalem_adi_sadelestir'deki NFC
    notu): birlesen nokta asagi akista kelimeyi ORTADAN boluyor."""
    if buyut:
        return {"i": "İ", "ı": "I"}.get(harf, harf.upper())
    return {"I": "ı", "İ": "i"}.get(harf, harf.lower()).replace("̇", "")


# Dizgi tek başına kategoriyi %68,4 doğrulukla veriyordu (taban %54,6): yetersiz
# %22 büyük/%8 nokta, yeterli %78/%99,5. `_dizgi_carpit` örnek üzerinden dolaylı
# çalışıyor ve yetmiyor (%30 çevirme -> çıktıda %8). Burada son işlem olarak
# doğrudan hedef dağılıma çekilir; simülasyonda %68,4 -> %55,7.
# ai_uretimi KASITLI dışarıda: %100 resmî dizgi onun TANIMI, artefakt değil.
_DIZGI_HEDEFI: dict[str, tuple[float, float]] = {
    # kategori: (büyük harfle başlama olasılığı, nokta ile bitme olasılığı)
    "yeterli": (0.70, 0.80),
    "yetersiz": (0.45, 0.40),
    "manipulatif": (0.70, 0.65),
}


def dizgi_normalize(metin: str, kategori: str) -> str:
    """Baş harf / son nokta dizgisini kategorinin hedef dağılımına çeker.

    Yalnız '.' eklenip çıkarılır; '!' ve '?' anlam taşır, dokunulmaz.
    Türkçe baş harf için `_tr_bas_harf` şart."""
    hedef = _DIZGI_HEDEFI.get(kategori)
    if not metin or hedef is None:
        return metin
    p_buyuk, p_nokta = hedef
    s = metin.strip()
    if not s:
        return metin
    s = _tr_bas_harf(s[0], random.random() < p_buyuk) + s[1:]
    if random.random() < p_nokta:
        if s[-1:] not in ".!?":
            s += "."
    elif s[-1:] == ".":
        s = s[:-1].rstrip()
    return s


def _dizgi_carpit(ornek: str) -> str:
    """Ornegin buyuk-harf/son-nokta dizgisini olasilikla TERSINE cevirir."""
    if not ornek or random.random() >= _DIZGI_CEVIRME_ORANI:
        return ornek
    resmi = ornek[0].isupper() and ornek.rstrip()[-1:] in ".!?"
    if resmi:                       # resmi -> gundelik
        s = _tr_bas_harf(ornek[0], False) + ornek[1:]
        return s.rstrip().rstrip(".!?")
    s = _tr_bas_harf(ornek[0], True) + ornek[1:]        # gundelik -> resmi
    return s if s.rstrip()[-1:] in ".!?" else s.rstrip() + "."


def fewshot_blok(kategori: str, adet: int = 2, dal: str | None = None) -> str:
    havuz = _fewshot_havuz(kategori, dal)
    if not havuz:
        return ""
    secim = random.sample(havuz, min(adet, len(havuz)))
    satirlar = "\n".join(f"- {_dizgi_carpit(s)}" for s in secim)
    return f"Örnek TARZLAR (birebir kopyalama, yalnız tarzı yakala):\n{satirlar}\n"


# NOT: ilk iki satırın AYNI olması KASITLIDIR (hata değil). 8B'ye "kesinlikle böyle
# yap / asla şöyle yapma" gibi uç talimat vermek yerine istenen davranış ÖRNEK olarak
# sunuluyor; aynı örneği iki kez koymak o davranışın seçilme sıklığını (2/3) artırır.
YETERLI_USLUP_IPUCLARI = [
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "birinci ağızdan, kısa ve doğal bir cümle ('... için X aldım/ödedim' gibi)",
    "gündelik konuşma diliyle, fazla resmi olmayan bir ifade",
]

# yeterli İSKELE: iyi örnekler (#66/#87) hem iş amacı verir, hem firma adını doğal
# kullanır, hem de gerçek kalemi kısaca dahil eder. Tek kalıba çakılmamak için
# fatura başına rastgele bir YAPI seçilir. Asıl amaç: Türkçenin AMAÇ-SONUÇ /
# NEDEN-SONUÇ cümle kalıplarını ("...olduğu için", "bu yüzden", "...amacıyla",
# "...gerekiyordu") modele hatırlatıp mantıklı senaryolar kurdurmak -- 8B'de
# `yeterli`nin en güçlü çeşitlilik kaldıracı budur. {firma} çalışma anında doldurulur.
YETERLI_ISKELE = [
    "Önce iş amacını (kim için / neden) söyle, sonra {firma}'dan hangi gerçek kalemi aldığını belirt.",
    "Önce fişteki gerçek kalem(ler)i aldığını yaz, ardından bunu neden/hangi iş için yaptığını ekle.",
    "Bir iş olayına bağla (toplantı, bayi ziyareti, saha işi, sunum öncesi, ekip ihtiyacı gibi) ve o "
    "kapsamda {firma}'dan gerçek kalemi aldığını anlat.",
    "Kısa ana cümlede amaç+gerçek kalemi ver; istersen ikinci kısa cümlede küçük bir bağlam/gerekçe ekle.",
    "NEDEN-SONUÇ kur: önce ortaya çıkan ihtiyacı/durumu söyle ('... bitmişti', '... gerekiyordu'), "
    "sonra bu yüzden {firma}'dan neyi aldığını yaz.",
    "AMAÇ-SONUÇ kur: yapılacak işi/etkinliği söyle, ardından o iş için gerçek kalemi aldığını "
    "'...amacıyla / ...için' bağlacıyla bağla.",
]

# yeterli DAYANAK kontrolü için iş-amacı isim havuzu (metin bunlardan hiçbirini,
# gerçek kalemi ya da firma adını içermiyorsa 'dayanaksız/muğlak' sayılır).
# DİKKAT: eşleşme token-PREFIX'tir, substring DEĞİL -- eskiden 2 harflik "is" girdisi
# substring aranıyordu ve 'kişisel', 'sistem', 'bisiklet', 'danışman' gibi alakasız
# kelimelerde eşleşip kuralı fiilen DEVRE DIŞI bırakıyordu. Kısa/tehlikeli girdiler
# havuzdan çıkarıldı, yerlerine çok-kelimeli güvenli ifadeler kondu (aşağıda).
_YETERLI_AMAC_ISIMLERI = (
    # EKİP/KURUM ekseni
    "proje", "toplanti", "ziyaret", "musteri", "ekip", "etkinlik", "sunum", "bayi",
    "saha", "organizasyon", "lansman", "fuar", "egitim", "departman", "ofis",
    "misafir", "agirlama", "ikram", "servis", "sube", "calisan", "personel",
    "sirket", "birim", "sevkiyat", "kurulum", "denetim", "seyahat", "stajyer",
    "rapor", "sozlesme", "teslimat", "sevk",
    # BİREYSEL eksen -- olay havuzuna BIREYSEL_OLAY eklendiğinde bu sözcükler
    # olmadan kural, kendi ürettiğimiz doğru örnekleri ihlal sayardı
    # ("Otoparka bıraktım, sabahki randevuya yetişmem gerekiyordu." gibi).
    "randevu", "gorusme", "mesai", "vardiya", "gorev", "mulakat", "oryantasyon",
    "sprint", "kampanya", "stok", "bakim", "ariza", "sayim", "teslim", "konaklama",
    "prototip", "sertifika", "danismanlik",
    # DEĞİŞMEZ: üretimde kullanılan HER olay havuzu girdisi bu kuralı geçmeli.
    # Ölçüldü (2026-07-28): 134 girdinin 34'ü geçemiyordu -> model bizim verdiğimiz
    # bağlamı yazıyor, kural onu 'amaçsız' sayıyor, retry DÜZELTEMİYOR (saf israf).
    # Aşağıdaki kökler o boşluğu kapatır; havuza yeni olay eklerken kontrol et.
    "transfer", "havalimani", "yakit", "arac", "otopark", "adaptor", "kablo",
    "aksesuar", "sarj", "lisans", "guncelleme", "yedekleme", "mevzuat", "arsiv",
    "depo", "sezon", "kapanis", "kirtasiye", "defter", "yetkinlik", "dizustu",
    "filo", "mudahale", "duzen", "temizlik", "tedarik", "hazirlik", "yenileme",
    "cikis", "donus", "alan", "kosul", "surec", "iyilestirme", "operasyon",
    "sarf", "baglanti", "calisma",
)

# Substring aranabilecek kadar güvenli çok-kelimeli iş-amacı ifadeleri.
_YETERLI_AMAC_IFADELERI = (
    "is icin", "isle ilgili", "is gereg", "is seyahat", "is toplanti",
    # bireysel bağlam kalıpları (fiil/durum -- tek kelimelik kök vermiyor)
    "ise gel", "ise gid", "gec kal", "yetis", "yol ust", "sabah erken",
    "aksam gec", "tek basima", "kendi masam", "mesaiye kal", "gorev sirasinda",
)


def _amac_koku_var_mi(n_tok: set[str]) -> bool:
    """Token'ların herhangi biri bir iş-amacı köküne indirgeniyor mu?

    Neden düz prefix YETMİYOR: Türkçede ünsüz yumuşaması var, 'ekip' -> 'ekibine'
    olur ve `startswith('ekip')` tutmaz. Ölçüldü: 'Üretim ekibine antrepo hizmetleri
    aldım.' geçerli bir yeterli olduğu hâlde kural onu DAYANAKSIZ sayıyordu.
    Çözüm: her token için sondan kısaltarak aday kökler üretilir ve son harfin
    yumuşaması geri alınır (b->p, c->ç, d->t, g/ğ->k)."""
    yumusama = {"b": "p", "c": "c", "d": "t", "g": "k"}
    for tok in n_tok:
        for boy in range(len(tok), 3, -1):     # en az 4 harflik kök
            aday = tok[:boy]
            if aday in _YETERLI_AMAC_ISIMLERI:
                return True
            sert = aday[:-1] + yumusama.get(aday[-1], aday[-1])
            if sert in _YETERLI_AMAC_ISIMLERI:
                return True
    return False

# ai_uretimi kategorisinde model tek bir kapanış kalıbına ("...kapsamında
# gerçekleştirilmiştir") aşırı yakınsıyordu (200 örnekte %70). Kapanışı
# fatura başına rastgele seçip zorunlu kılarak kalıp çeşitliliğini garantiye
# alıyoruz.
# Havuz 6 -> 22: 20k'lık üretimde ai_uretimi payı ~%10 (~2000 metin) olduğundan
# 6 kapanış, kapanış başına ~330 tekrar demekti (collapse garantisi). 22'de ~90'a iner.
# NOT: belgeyi/süreci anlatan kapanışlar ("belgelendirilmiştir", "raporlanmıştır")
# bilerek DIŞARIDA -- açıklama harcamayı anlatır, fişin kendisini değil.
AI_URETIMI_KAPANIS_IPUCLARI = [
    "kapsamında gerçekleştirilmiştir",
    "için işlem yapılmıştır",
    "amacıyla gerçekleştirilmiştir",
    "doğrultusunda sağlanmıştır",
    "kapsamında temin edilmiştir",
    "çerçevesinde gerçekleştirilmiştir",
    "kapsamında karşılanmıştır",
    "doğrultusunda temin edilmiştir",
    "amacıyla tedarik edilmiştir",
    "çerçevesinde karşılanmıştır",
    "kapsamında tanzim edilmiştir",
    "doğrultusunda değerlendirilmiştir",
    "amacıyla kayıt altına alınmıştır",
    "çerçevesinde uygun görülmüştür",
    "kapsamında talep edilmiştir",
    "doğrultusunda onaya sunulmuştur",
    "çerçevesinde işleme alınmıştır",
    "kapsamında gider olarak kaydedilmiştir",
    "doğrultusunda gerekli görülmüştür",
    "amacıyla sağlanmıştır",
    "çerçevesinde temin edilmiştir",
    "doğrultusunda tamamlanmıştır",
]

# ai_uretimi AÇILIŞ havuzu: tüm çıktılar 'Belirtilen fiş kapsamında gerçekleştirilen
# harcamalar...' ile başlıyordu (deneme_80'de 20/20). Kaynak, ai talimatındaki sabit
# örnek ifadeydi -> model onu birebir açılış yapıyordu. Açılışı fatura başına rastgele
# döndürerek collapse'ı kırıyoruz (kapanış havuzuyla aynı mantık).
AI_URETIMI_ACILIS_IPUCLARI = [
    "İlgili masraf kalemi",
    "Söz konusu harcama",
    "İşbu masraf",
    "Fişe konu kalemler",
    "Bahse konu giderler",
    "Kayda alınan masraf",
    "Değerlendirilen harcama kalemleri",
    "Belirtilen fiş kapsamında gerçekleştirilen harcamalar",
    "Mevcut gider kalemi",
    "Sunulan masraf belgesindeki harcama",
    "Yukarıda belirtilen gider",
    "Tarafımca gerçekleştirilen harcama",
    "İlgili döneme ait masraf",
    "Söz konusu tedarik işlemi",
    "Beyan edilen gider kalemleri",
    "İncelenen harcama",
    "Anılan masraf kalemi",
    "Gerçekleştirilen alım",
    "İlgili birim tarafından yapılan harcama",
    "Bahsi geçen gider kalemi",
]

# yetersiz kategorisinde de benzer şekilde "...temin edilmiştir" kalıbına
# yakınsama vardı (%22). Üslup çeşitliliği için rastgele seçim.
YETERSIZ_USLUP_IPUCLARI = [
    "sadece firma adını an, iş amacından hiç bahsetme",
    "'ihtiyaç için', 'iş ile ilgili' gibi çok genel geçer ifadelerle geç",
    "hangi ürün/hizmet olduğunu belirtme, sadece genel bir harcama olduğunu söyle",
    "kısa ve detaysız, tek bir öbek halinde",
    "'çeşitli', 'genel', 'muhtelif' gibi belirsizlik bildiren kelimelerle",
    # Yetersiz her zaman kalemden kaçmaz; bazen kalemi/firmayı ANAR ama BAĞLAM vermez
    # ("ABC ile toplantı gideri" der, "DEF projesi için ekiple yemek" demez).
    "fişteki bir kalemi ya da firmayı an ama neden alındığını söyleme "
    "('X ile toplantı gideri', 'Y karşılandı' gibi bağlamsız)",
]

# yetersiz talimatındaki literal örnekler SABİT verildiğinde model bunları birebir
# kopyalıyordu ('genel ofis ihtiyacı' 5'te 4). Havuzdan her seferinde 2 rastgele
# örnek gösterip döndürerek collapse'ı kırıyoruz (few-shot ise yetersiz'de artık
# hiç kullanılmıyor -- talimat örnekleriyle çakışıp verbatim kopyayı besliyordu).
YETERSIZ_ORNEK_HAVUZ = [
    # isim-öbeği (muğlak, kategorik)
    "genel ofis ihtiyacı", "iş gideri", "muhtelif harcama", "departman ihtiyacı",
    "çeşitli alışveriş", "rutin harcama", "genel gider", "ihtiyaç malzemesi",
    "ofis için", "gerekli malzeme", "aylık ihtiyaç", "olağan gider",
    "işle ilgili alım", "standart harcama", "birim gideri",
    # üşengeç/baştan-savma cümle (whaatif'in en iyi verdiği ton)
    "masraf", "gider", "bir şeyler aldım işte", "gerekliydi aldım",
    "iş için lazımdı", "çeşitli şeyler işte", "toplantı için bir şeyler", "gerekliydi işte",
    "lazımdı aldım", "iş icabı", "ihtiyacım oldu", "böyle bir masraf çıktı",
    "gerekiyordu", "aldım işte", "iş kaynaklı",
    # kalemi/firmayı anan ama BAĞLAMSIZ (yeni üslup seçeneğiyle uyumlu)
    "market alışverişi", "yemek gideri", "ulaşım masrafı", "kırtasiye alımı",
    # 34 -> 60: havuz dar olduğu için prompt'ta gösterilen 2 örnek sık sık aynı
    # girdilere denk geliyor ve model onları birebir geri veriyordu (qwen3 pilotunda
    # 8 yetersizin 3'ü tek bir ifadeye çöktü). Geniş havuz + tam-eşleşme ihlali
    # (bkz. _verbatim_kopya_mi) birlikte çalışır.
    "alım yapıldı", "gerekli görüldü", "işin gereği", "normal harcama",
    "günlük ihtiyaç", "ekip için alındı", "sarf malzemesi", "ufak tefek şeyler",
    "acil ihtiyaçtı", "listede vardı", "her zamanki gibi", "yine aynı ihtiyaç",
    "bu ay da bunlar", "zaten gerekiyordu", "onaya sunuyorum", "harcama kaydı",
    "iş için alışveriş", "gerekli şeyler", "birkaç kalem", "mutat gider",
    "dönemsel alım", "prosedür gereği", "talep üzerine alındı", "işte bu kadar",
    "detay yok", "not düşecek bir şey yok", "sıradan bir gider",
]

# manipulatif "aşırı haklı çıkarma" dalı: model bazen abartılı vurguyu atlayıp
# meşru/yeterli gibi okunan cümle üretiyordu. Zorunlu bir vurgu açılışı seçip
# dayatarak (ve validator'la denetleyerek) manipülatif "işareti"ni garantiye
# alıyoruz. _VURGU_ANAHTARLARI, üretilen metinde vurgunun varlığını (Türkçe
# normalize edilmiş) yakalamak için kullanılır.
# Havuz 16 -> 40: `vurgu_eksik` bir İHLAL (retry tetikler), yani bariz dalda vurgu
# GARANTİ metne giriyor. Bu kasıtlı (kalın çizgi: model manipülatif işaretini önce
# net öğrensin) ama dar havuz "kesinlikle => manipulatif" gibi ezberlenebilir bir
# kısayol bırakır. ~1200 bariz metinde 16 vurgu = ~75 tekrar; 40'ta ~30'a iner.
MANIPULATIF_VURGU_IPUCLARI = [
    "Kesinlikle",
    "Yüzde yüz iş amaçlı",
    "Tamamen iş gereği",
    "Hiç şüphesiz iş için",
    "Yüzde yüz proje kapsamında",
    "Hiç kuşkusuz iş amaçlı",
    "Kesinlikle şirket gideri",
    "Tümüyle iş için",
    # 'kesinlikle/yüzde yüz/tamamen' aşırı tekrar ediyordu -> emphatic ifadeyi çeşitlendir
    # (aynı zamanda 'kelime = manipulatif' sahte sinyalini de zayıflatır).
    "Hiç tartışmasız iş için",
    "Net biçimde işle ilgili",
    "Gönül rahatlığıyla iş amaçlı",
    "Baştan sona iş kapsamında",
    "Her açıdan işe dönük",
    "Eksiksiz biçimde proje için",
    "Şüpheye yer yok, iş amaçlı",
    "Bal gibi iş gideri",
    "Katiyen özel bir harcama değil",
    "Sonuna kadar iş kaynaklı",
    "Apaçık şirket masrafı",
    "İstisnasız iş kapsamında",
    "Baştan aşağı kurumsal",
    "Bire bir işle bağlantılı",
    "Tereddütsüz proje gideri",
    "Elbette ki iş amaçlı",
    "Açık açık iş için",
    "Kılı kırk yararak söylüyorum, iş için",
    "Dosdoğru iş amaçlı",
    "Adı üstünde şirket gideri",
    "Kesin olarak departman ihtiyacı",
    "Yüzde yüz ekip gideri",
    "Bütünüyle iş kapsamında",
    "Tamamen kurumsal gereklilik",
    "Hiç abartısız iş için",
    "Salt iş amaçlı",
    "Yalnızca ve yalnızca iş için",
]
# Genel emphatic-marker fallback (atanan vurgu paraphrase edilse de yakalansın).
# HAVUZLA SENKRON TUTULMALI: her MANIPULATIF_VURGU_IPUCLARI öğesi buradaki bir kökle
# ya da kendi ayırt edici kelimesiyle yakalanabilmeli (_vurgu_var_mi önce atanan
# vurgunun kelimelerine, sonra bu listeye bakar).
_VURGU_ANAHTARLARI = (
    "kesinlikle", "yuzde yuz", "tamamen", "tumuyle", "suphesiz", "kuskusuz",
    "tartismasiz", "eksiksiz", "gonul rahat", "bastan sona", "her acidan",
    "net bicimde", "bal gibi", "katiyen", "sonuna kadar",
    "apacik", "istisnasiz", "bastan asagi", "bire bir",
    "tereddut", "elbette", "acik acik", "kili kirk",
    "dosdogru", "adi ustunde", "kesin olarak", "butunuyle",
    "kurumsal gereklilik", "abartisiz", "salt is", "yalnizca ve yalnizca",
)

# Vurgunun cümledeki KONUMU rastgele seçilir: menü ("başa/ortaya/sona") verilince
# model hep başa koyuyordu -> eğitimde sahte konumsal sinyal. Tek somut slot dayatmak
# konumu fatura başına değiştirir, dağılımı dengeler.
MANIPULATIF_VURGU_KONUMLARI = ["cümlenin BAŞINA", "cümlenin ORTASINA", "cümlenin SONUNA"]

# ---------------------------------------------------------------------------
# PERSONA (Faz 7'de geri getirildi): whaatif'in karakter zenginliğini taşır.
# 8B'de lexical ÇEŞİTLİLİĞİ az artırır (bkz. arXiv:2505.17390) ama KARAKTER/ses
# için değerli -- 'umursamaz/yorgun/aceleci bir kişi gibi' yazmak çıktıya insan
# tınısı verir; yüksek parametreli modellerde etkisi daha da büyür. SADECE user
# prompt'a girer (system cache korunur). ai_uretimi'ne UYGULANMAZ (robotik kalmalı).
# ---------------------------------------------------------------------------

PERSONA_ROL = ["satış temsilcisi", "yazılım mühendisi", "saha teknisyeni",
               "yönetici asistanı", "muhasebe uzmanı", "proje yöneticisi",
               "pazarlama uzmanı", "insan kaynakları uzmanı", "lojistik sorumlusu",
               "operasyon uzmanı"]
PERSONA_KIDEM = ["yeni başlamış", "orta seviye", "kıdemli", "departman yöneticisi"]
PERSONA_RUH = ["aceleci", "sakin", "yorgun", "dikkatli", "umursamaz", "stresli"]
PERSONA_YAZIM = [
    "düzgün yazım",
    "hep küçük harf, noktalama az",
    "arada ufak yazım/daktilo hatası olan",
    "kısa ve devrik cümleli",
    "resmi ve düzgün noktalamalı",
]


def persona_uret() -> dict:
    return {
        "rol": random.choice(PERSONA_ROL),
        "kidem": random.choice(PERSONA_KIDEM),
        "ruh": random.choice(PERSONA_RUH),
        "yazim": random.choice(PERSONA_YAZIM),
    }


# ---------------------------------------------------------------------------
# DEPARTMAN + OLAY + ÖLÇEK -- açıklamanın AMAÇ çıpası
# ---------------------------------------------------------------------------
# Neden: `yeterli`nin tanımı gereği zorunlu unsuru "hangi iş bağlamı" ama prompt bunu
# yalnız soyut olarak istiyordu; T1 pilotunda bir üretim salt kalem listesi çıktı
# ("Barbekü Soslu Tavuk, Tavuklu Pizza, Hot Dog ve Ispanaklı Gözleme aldım.") ve kural
# bunu ihlal saymadı. Somut olay havuzu hem amacı garantiler hem senaryo çeşitliliğini
# BİZİM kontrolümüze alır (model uydurmasına değil).
#
# Departman personadan TÜRETİLİR, bağımsız rastgele slot DEĞİL: 'satış temsilcisi' +
# 'Ar-Ge departmanı' çelişkisi 8B'de karakteri bozar (bkz. çelişki yasağı, docs/arsiv/faz-b-prompt.md §6).
ROL_DEPARTMAN = {
    "satış temsilcisi":        "Satış",
    "yazılım mühendisi":       "Ar-Ge",
    "saha teknisyeni":         "Saha Operasyon",
    "yönetici asistanı":       "İdari İşler",
    "muhasebe uzmanı":         "Finans",
    "proje yöneticisi":        "Proje Yönetimi",
    "pazarlama uzmanı":        "Pazarlama",
    "insan kaynakları uzmanı": "İnsan Kaynakları",
    "lojistik sorumlusu":      "Lojistik",
    "operasyon uzmanı":        "Operasyon",
}

# 18 harcama kategorisi -> 10 olay grubu (tam çapraz çarpım 180 hücre olurdu).
# Yasaklı kategoriler (alkol/eglence/tutun/kumar) ve 'diger' burada YOK -> 'genel'e düşer.
KATEGORI_GRUBU = {
    "yemek_hizmeti": "yemek", "temel_gida": "yemek",
    "ulasim_hizmeti": "ulasim", "ulasim_bireysel": "ulasim",
    "konaklama": "konaklama",
    "ofis_sarf_malzeme": "ofis", "ofis_mobilya": "ofis",
    "teknoloji_ekipman": "teknoloji", "yazilim_lisans": "teknoloji",
    "danismanlik": "hizmet",
    "giyim": "giyim", "kisisel_bakim": "bakim", "temizlik": "temizlik",
}

# ÖLÇEK: harcamayı ekip/kurum için mi, çalışan kendisi için mi yaptı? Departmandan
# BAĞIMSIZ ikinci boyut. Bireysel senaryolar tek kalemli küçük fişleri doğal kılar ve
# "hep ekip" tekdüzeliğini kırar (T1 pilotunda 8 yeterli'nin 6'sı ekip bağlamındaydı).
OLCEK_AGIRLIK = {                # (bireysel, ekip)
    "ulasim":    (0.75, 0.25),   # taksi/yakıt/otopark doğası gereği bireysel
    "bakim":     (0.70, 0.30),
    "konaklama": (0.60, 0.40),
    "giyim":     (0.50, 0.50),
    "teknoloji": (0.40, 0.60),
    "genel":     (0.40, 0.60),
    "yemek":     (0.35, 0.65),
    "hizmet":    (0.30, 0.70),
    "ofis":      (0.25, 0.75),
    "temizlik":  (0.15, 0.85),
}

# Her departmanda geçerli, kalem grubuna bağlı EKİP olayları.
GRUP_OLAY = {
    "yemek":      ["ekip öğle yemeği", "müşteri ağırlama", "toplantı ikramı",
                   "saha ekibine mola", "mesai sonrası çalışma yemeği", "eğitim günü ikramı",
                   "vardiya değişimi ikramı", "yoğun sezon desteği", "ziyaretçi kahvaltısı",
                   "ay sonu kapanış çalışması"],
    "ulasim":     ["şehir içi müşteri ziyareti", "havalimanı transferi",
                   "saha noktasına gidiş", "fuar alanına ulaşım", "şube turu",
                   "acil müdahale çağrısı", "eğitim merkezine gidiş"],
    "konaklama":  ["şehir dışı görev", "fuar katılımı", "bayi ziyareti", "saha kurulumu",
                   "bölge toplantısı", "denetim ziyareti"],
    "ofis":       ["ofis stoğunun bitmesi", "yeni çalışan kurulumu",
                   "toplantı odası hazırlığı", "ay sonu raporlama", "arşiv düzenleme",
                   "eğitim materyali hazırlığı", "yeni şube açılışı"],
    "teknoloji":  ["ekipman arızası", "yeni çalışan başlangıcı", "sunum hazırlığı",
                   "lisans yenileme", "sistem güncellemesi", "yedekleme ihtiyacı",
                   "uzaktan çalışma kurulumu"],
    "hizmet":     ["dönemsel danışmanlık", "denetim hazırlığı", "süreç iyileştirme çalışması",
                   "mevzuat değişikliği", "sertifikasyon süreci"],
    "giyim":      ["saha görevi", "fuar standı görevi", "tanıtım etkinliği",
                   "yeni sezon hazırlığı", "kurumsal etkinlik"],
    "bakim":      ["ofis kiti tamamlama", "misafir hazırlığı", "saha ekibi ihtiyacı",
                   "sosyal alan düzeni", "etkinlik hazırlığı"],
    "temizlik":   ["ofis temizlik ihtiyacı", "etkinlik sonrası toparlanma", "ortak alan bakımı",
                   "depo düzenleme", "sezon başı temizliği"],
    "genel":      ["departman ihtiyacı", "ekip talebi", "operasyon planlaması",
                   "acil müdahale", "rutin tedarik"],
}

# Olayın prompt'a girerken aldığı ÇERÇEVE. Model olay ifadesini büyük ölçüde aynen
# taşıyor (qwen3_3: bağlam alan çıktıların hepsi 'Denetim hazırlığı için...',
# 'Sahada öğle molası için...' diye kopyaladı). Çekirdek isim tekrarı sorun değil --
# amaç sözcüğünün tekrarlaması gerçek veride de olur -- ama cümlenin TAMAMI aynı
# kalıba oturmasın diye çerçeve rastgele değişir.
# DİKKAT: çerçeve yalnız EKİP/zorunluluk dalında kullanılır. Bireysel olayların bir
# kısmı kendi içinde 'için' taşıyor ('evden çalışma için kırtasiye') ve çerçeveyle
# birleşince bozuk Türkçe çıkıyordu ('... için kırtasiye sırasında yaptın').
OLAY_CERCEVELERI = [
    # EŞZAMANLILIK kipleri: harcama olayın PARÇASI değil, olay SIRASINDA yapılmış olabilir.
    # Yalnız aidiyet kipi ('X kapsamında gerekli') verilirse model yanlış bir doğrudan
    # ilişki kuruyor -- "Sistem entegrasyonu kapsamında gerekli yemek hizmetleri" gibi.
    # Eşleşmenin kendisi meşru (entegrasyon yapan ekip yemek yer), kusurlu olan bağlaç.
    "{olay} sırasında", "{olay} çalışması sürerken", "{olay} günü", "{olay} devam ederken",
    # AİDİYET kipleri
    "{olay} nedeniyle", "{olay} öncesinde", "{olay} kapsamında", "{olay} için",
    "{olay} sonrasında", "{olay} planlandığı için",
]

# ---------------------------------------------------------------------------
# YÜKLEM HAVUZU -- 'aldım' tekelini kırar
# ---------------------------------------------------------------------------
# Üç koşuda da ölçüldü: cümlelerin ~%20'si 'aldım' ile bitiyor, gerisi renksiz
# kurumsal fiillere kayıyor (karşıla/temin/sağlan 12/32). Kaleme uygun yüklem
# collocation'ı modelin zayıf tarafı: "bulabildiğimiz taksi hizmetini kullanmak
# zorunda kaldım" yerine "çağırdığımız taksi" daha doğal. Öneri ZORLAMA değil:
# üretim başına rastgele TEK bir fiil verilir, model kullanmak zorunda değil.
GRUP_YUKLEM = {
    "yemek":     ["ısmarladım", "söyledim", "yedik", "ikram ettim", "aldım"],
    "ulasim":    ["çağırdım", "bindim", "ayarladık", "kullandım", "ödedim"],
    "konaklama": ["kaldım", "konakladık", "rezervasyon yaptırdım", "ödedim"],
    "ofis":      ["aldım", "temin ettim", "sipariş verdim", "tamamladım"],
    "teknoloji": ["aldım", "değiştirdim", "kurdurdum", "temin ettim"],
    "hizmet":    ["hizmet aldık", "danıştık", "sözleşme yaptık", "aldım"],
    "giyim":     ["aldım", "temin ettik", "hazırlattım"],
    "bakim":     ["aldım", "tamamladım", "temin ettim"],
    "temizlik":  ["aldım", "temin ettim", "getirttim"],
    "genel":     ["aldım", "ödedim", "karşıladım", "temin ettim"],
}


def yuklem_ipucu(baskin_kategori_ham: str) -> str:
    grup = KATEGORI_GRUBU.get(baskin_kategori_ham, "genel")
    return random.choice(GRUP_YUKLEM.get(grup, GRUP_YUKLEM["genel"]))


# ---------------------------------------------------------------------------
# MANİPÜLATİF KILIF HAVUZU -- kurnaz/bariz dallarının içerik çıpası
# ---------------------------------------------------------------------------
# `yeterli`nin olay havuzundan İKİ FARKLA ayrılır:
#   (a) kaleme KOŞULLU DEĞİL -- sıradan kalemi büyük laflarla sunmak zaten kategorinin
#       tanımı, uyumsuzluk kasıtlı;
#   (b) kayıt şişirilmiş kurumsal, somut/gerçekçi değil.
# Neden gerekli: çıpasız bırakılan kurnaz dal kendi kılıfını uydururken SPESİFİK teknik
# iddialar kuruyordu ("Sistem entegrasyonu kapsamında gerekli yemek hizmetleri"). Genel
# bir şişirme ifadesi her kaleme takılabilir, absürt olmaz ama manipülatif kalır.
MANIPULATIF_KILIF_HAVUZU = [
    # kurumsal şişirme
    "stratejik değerlendirme", "yönetsel istişare", "üst düzey koordinasyon",
    "kurumsal temsil", "temsil gideri", "protokol gereği", "yıllık plan görüşmesi",
    "yönetim kademesi çalışması", "kritik karar süreci", "yol haritası çalışması",
    # süreç dili
    "süreç iyileştirme", "operasyonel süreklilik", "verimlilik çalışması",
    "kapasite planlaması", "kalite güvence adımı", "risk azaltma tedbiri",
    "iş sürekliliği gereği", "entegrasyon çalışması", "saha uyum çalışması",
    "performans takibi",
    # ilişki / itibar dili
    "paydaş ilişkileri", "müşteri memnuniyeti çalışması", "iş geliştirme faaliyeti",
    "kurumsal itibar yönetimi", "ilişki yönetimi", "networking faaliyeti",
    "iş ortaklığı görüşmesi", "marka temsili", "misafir protokolü",
    "kurumsal görünürlük",
    # motivasyon / insan dili
    "ekip motivasyonu programı", "çalışan bağlılığı çalışması", "moral bütçesi",
    "takım ruhu etkinliği", "iç iletişim çalışması", "kültür geliştirme faaliyeti",
    "yatırım öncesi hazırlık", "maliyet optimizasyonu çalışması",
]

# GİDER-KALEMİ dilindeki kılıflar AYRI tutulur: bunlar bir OLAY değil bir muhasebe
# etiketidir, zaman kipiyle birleşince bozuk çıkıyor ('temsil gideri sırasında').
# Yalnız aidiyet çerçevesi alırlar.
MANIPULATIF_KILIF_KALEM = [
    "temsil gideri", "faaliyet gideri", "genel yönetim gideri", "zorunlu gider kalemi",
    "dönemsel gider kalemi", "bütçe kalemi", "onaylı harcama planı",
    "departman bütçe kullanımı", "planlanmış tedarik",
]

# KILIF <-> KATEGORİ UYUMU (2026-07-30). Önce kılıf havuzu KATEGORİDEN BAĞIMSIZ
# seçiliyordu ve `kilif_notu_uret` kategoriyi parametre olarak ALIP KULLANMIYORDU.
# Sonuç, kullanıcı incelemesindeki "alakasız kelimeleri yan yana getirmiş" vakası:
#     'Taksim Hamburger'den alınan yemek, YÖNETİM KADEMESİ ÇALIŞMASI sürerken
#      sağlanan destek.'
#     'Kuşku Etkinlik'ten aldığım ürünleri BÜYÜK BİR HEDEFE ULAŞMAK adına harcadım.'
# Beğenilen örneklerin ortak yanı ise kılıfın kalemden TÜREMESİ:
#     'API kullanım ücreti, SİSTEM ENTEGRASYON SÜRECİNDE ... belirlenmiş bir maliyet'
#
# KILIF ÜRÜN ADI İÇERMEZ (CLAUDE.md §8): kategoriye KOŞULLANIR, kalem adını
# tekrarlamaz -- havuza ürün adı konduğunda model fişte olmayan ürünü almış gibi
# yazıyordu. Buradaki tüm girdiler soyut kurumsal ifadeler.
#
# Her kategori KILIF_GENEL ile birleştiği için havuz 14-18 arasında kalır;
# collapse riski yok (§8: "sabit kalıp collapse üretir, havuz üretmez").
KILIF_GENEL = [
    "operasyonel süreklilik", "iş sürekliliği gereği", "süreç iyileştirme",
    "verimlilik çalışması", "performans takibi", "maliyet optimizasyonu çalışması",
]
KILIF_KATEGORI: dict[str, list[str]] = {
    "yemek_hizmeti": ["ekip motivasyonu programı", "takım ruhu etkinliği", "moral bütçesi",
                      "misafir protokolü", "iş ortaklığı görüşmesi", "müşteri memnuniyeti çalışması",
                      "paydaş ilişkileri", "kurumsal temsil", "protokol gereği",
                      "yıllık plan görüşmesi", "iç iletişim çalışması"],
    "temel_gida": ["ekip motivasyonu programı", "takım ruhu etkinliği", "moral bütçesi",
                   "misafir protokolü", "iç iletişim çalışması", "kültür geliştirme faaliyeti",
                   "kurumsal temsil", "müşteri memnuniyeti çalışması"],
    "konaklama": ["yıllık plan görüşmesi", "iş ortaklığı görüşmesi", "üst düzey koordinasyon",
                  "saha uyum çalışması", "kurumsal temsil", "stratejik değerlendirme",
                  "protokol gereği", "kritik karar süreci"],
    # Süreç dili (operasyonel süreklilik, süreç iyileştirme, verimlilik...) YEMEK ve
    # KONAKLAMA için uyumsuzdu ama ULAŞIM/TEKNOLOJİ/OFİS için doğal. Bu yüzden
    # jenerik havuzu her kategoriye eklemek yerine UYDUĞU kategorilere dağıtıldı;
    # her liste KILIF_OZEL_YETERLI_ESIK'e (8) ulaştığı için artık jenerik eklenmiyor.
    "ulasim_hizmeti": ["saha uyum çalışması", "üst düzey koordinasyon", "kapasite planlaması",
                       "entegrasyon çalışması", "iş ortaklığı görüşmesi",
                       "operasyonel süreklilik", "iş sürekliliği gereği", "performans takibi"],
    "ulasim_bireysel": ["saha uyum çalışması", "üst düzey koordinasyon", "performans takibi",
                        "iş ortaklığı görüşmesi", "kritik karar süreci",
                        "operasyonel süreklilik", "iş geliştirme faaliyeti", "networking faaliyeti"],
    "ofis_sarf_malzeme": ["kapasite planlaması", "kültür geliştirme faaliyeti",
                          "iç iletişim çalışması", "kalite güvence adımı", "yol haritası çalışması",
                          "süreç iyileştirme", "verimlilik çalışması", "operasyonel süreklilik"],
    "ofis_mobilya": ["kapasite planlaması", "kültür geliştirme faaliyeti", "yatırım öncesi hazırlık",
                     "çalışan bağlılığı çalışması", "kurumsal görünürlük",
                     "verimlilik çalışması", "süreç iyileştirme", "iç iletişim çalışması"],
    "teknoloji_ekipman": ["entegrasyon çalışması", "risk azaltma tedbiri", "kalite güvence adımı",
                          "yatırım öncesi hazırlık", "kapasite planlaması",
                          "iş sürekliliği gereği", "süreç iyileştirme",
                          "maliyet optimizasyonu çalışması"],
    "yazilim_lisans": ["entegrasyon çalışması", "risk azaltma tedbiri", "kalite güvence adımı",
                       "yol haritası çalışması", "stratejik değerlendirme",
                       "iş sürekliliği gereği", "süreç iyileştirme",
                       "maliyet optimizasyonu çalışması"],
    "danismanlik": ["stratejik değerlendirme", "yönetsel istişare", "yol haritası çalışması",
                    "kritik karar süreci", "risk azaltma tedbiri", "yıllık plan görüşmesi",
                    "kapasite planlaması", "maliyet optimizasyonu çalışması"],
    "kisisel_bakim": ["kurumsal itibar yönetimi", "marka temsili", "misafir protokolü",
                      "kurumsal görünürlük", "saha uyum çalışması", "kurumsal temsil",
                      "protokol gereği", "ilişki yönetimi"],
    "temizlik": ["kurumsal itibar yönetimi", "misafir protokolü", "kurumsal görünürlük",
                 "saha uyum çalışması", "kalite güvence adımı",
                 "kurumsal temsil", "iç iletişim çalışması", "operasyonel süreklilik"],
    "giyim": ["kurumsal temsil", "marka temsili", "kurumsal görünürlük", "protokol gereği",
              "misafir protokolü", "kurumsal itibar yönetimi",
              "ilişki yönetimi", "networking faaliyeti"],
}


# ZORUNLULUK GEREKÇESİ <-> KATEGORİ UYUMU (2026-07-30). Talimat eskiden SABİT bir
# menü veriyordu ("tek uygun seçenek oydu, acil ihtiyaçtı, başka yer/zaman yoktu,
# iş bekleyemezdi") ve kategoriden bağımsızdı. Kullanıcı incelemesindeki vaka:
#     'Oğulbaş Arslan Pansiyonu'dan kiraladık ... İŞ BEKLEYEMEZDİ, zorunda kaldık.'
# Mantık TERS: acil iş neden GECELEMEYİ gerektirsin? Konaklamanın doğru çerçevesi
# "iş uzadı, dönemedim"dir. Aynı menü kargo/taksi için doğru, konaklama için değil.
#
# Kalem kategorisine göre gerekçe iskeleti verilir; genel havuzla birleşir.
# HAVUZ GENİŞLETİLDİ (2026-07-30, 2. tur): ilk sürümde kategori başına 2-4 girdi
# vardı ve 48 manipulatiflik pilotta tekrar GÖRÜNÜR oldu -- "ekip sahada kaldı"
# iki kez, "başka alternatif/tedarikçi yoktu" üç kez. 25k'da belirgin kalıp olurdu.
ZORUNLULUK_GENEL = [
    "tek uygun seçenek oydu", "başka yer ya da zaman yoktu", "erteleyecek durum yoktu",
    "o an elde başka imkân yoktu", "işi durdurmamak için başka yol kalmamıştı",
]
ZORUNLULUK_GEREKCE: dict[str, list[str]] = {
    "konaklama": ["iş planlanandan uzun sürdü, dönecek vakit kalmadı",
                  "son görüşme geç bitti, o saatte yola çıkmak mümkün değildi",
                  "ertesi sabah erkenden yerinde olmam gerekiyordu",
                  "dönüş için uygun bir sefer kalmamıştı",
                  "hava koşulları dönüşü riskli hale getirmişti",
                  "program ertesi güne sarktı",
                  "sahadaki iş bir günde bitmedi"],
    "ulasim_bireysel": ["toplantıya yetişmem gerekiyordu",
                        "o saatte toplu taşıma yoktu",
                        "araç yolda kaldı, beklemek işi aksatacaktı",
                        "belgelerin aynı gün ulaştırılması gerekiyordu",
                        "randevu saati kaymaya müsait değildi",
                        "sahaya ulaşımın başka yolu yoktu"],
    "ulasim_hizmeti": ["sevkiyat söz verilen güne yetişmeliydi",
                       "yük bekletilirse üretim duracaktı",
                       "tek çıkan araç oydu",
                       "gümrük süresi doluyordu",
                       "müşteri teslim tarihini öne çekmişti",
                       "soğuk zincir bekletmeye uygun değildi"],
    "yemek_hizmeti": ["ekip sahada kaldı, yakında başka yer yoktu",
                      "toplantı öğlene sarktı, ara verilemedi",
                      "misafiri aç bekletemezdim",
                      "mesai uzayınca ekip yerinde kaldı",
                      "görüşme yemek saatine denk geldi",
                      "çevrede açık tek yer orasıydı"],
    "temel_gida": ["ofiste stok bitmişti, gün içinde tamamlanması gerekiyordu",
                   "yakında açık başka yer yoktu",
                   "misafir beklenirken hazırlık yapılması gerekiyordu",
                   "ertesi güne bırakmak işi aksatacaktı"],
    "teknoloji_ekipman": ["mevcut cihaz bozuldu ve iş durdu",
                          "yedeği yoktu, aynı gün temin etmek zorundaydım",
                          "arıza işi tamamen durdurmuştu",
                          "servise vermek günler alacaktı",
                          "teslim tarihine yetişmesi gerekiyordu"],
    "yazilim_lisans": ["lisans süresi doldu ve sistem kilitlendi",
                       "yenilemeyi bekletmek erişimi kesecekti",
                       "güvenlik açığı acilen kapatılmalıydı",
                       "ekip o gün çalışamaz hale gelmişti"],
    "ofis_sarf_malzeme": ["stok tükenmişti, iş aksıyordu",
                          "teslim tarihi yaklaşmıştı, beklemek mümkün değildi",
                          "baskı işi aynı gün çıkacaktı",
                          "sunum öncesi eksik tamamlanmalıydı"],
    "ofis_mobilya": ["mevcutları kullanılamaz haldeydi",
                     "yeni ekip aynı hafta başlıyordu",
                     "taşınma tarihi öne alınmıştı",
                     "çalışma alanı o hâliyle kullanılamıyordu"],
    "danismanlik": ["mevzuat değişikliği süre tanımıyordu",
                    "denetim tarihi yaklaşmıştı",
                    "başvuru penceresi kapanmak üzereydi",
                    "kurum içinde bu uzmanlık yoktu"],
    "kisisel_bakim": ["sahada hijyen koşulu vardı",
                      "denetim öncesi eksiklerin kapatılması gerekiyordu",
                      "müşteri ziyareti aynı güne alınmıştı"],
    "temizlik": ["denetim öncesi eksiklerin kapatılması gerekiyordu",
                 "ortak alan kullanılamaz haldeydi",
                 "ziyaret öncesi hazırlık yetiştirilmeliydi",
                 "temizlik malzemesi gün ortasında bitmişti"],
    "giyim": ["temsil gerektiren bir görev çıktı",
              "sahada koruyucu kıyafet zorunluydu",
              "etkinlik kıyafet kuralı getirmişti",
              "görev aynı gün bildirilmişti"],
}


# MAĞDURİYET DALI (2026-07-30) -- kullanıcının istediği beşinci manipulatif dal.
# Davranış: şirketin ödememesi gereken bir gideri MAĞDURİYET çerçevesine sığdırmak.
#     'Müşteri toplantısına yetişmek için taksiye binmek ZORUNDA KALDIM.'
#     'Görüşme sonrası hava çok yağışlıydı, EVE DÖNEMEDİM.'
# `zorunluluk`tan FARKI: orada gerekçe İŞİN kendisidir (iş uzadı, sevkiyat
# yetişmeliydi); burada gerekçe ÇALIŞANIN mağduriyetidir (mahsur kaldım, başıma
# geldi). İkisi de kaçınılmazlık kurar ama özne farklı.
#
# KRİTİK KISIT (kullanıcının kendi tespiti, doğru): yalnız ANOMALİLİ faturada ve
# yalnız çalışanın FARKINDA olabileceği türlerde açılır. Temiz faturada gerçek bir
# mağduriyet `manipulatif` sayılırsa `onay_durumu` (docs/04-etiketler.md §13) haksız
# 'red' verir -- etiketi gürültüye çevirirdi.
MAGDURIYET_TURLERI = {"limit_asimi", "yasakli_kategori", "mukerrer_fis_yukleme"}

MAGDURIYET_CERCEVELERI = [
    "planlanan dönüş saatini kaçırdım, mahsur kaldım",
    "hava koşulları yüzünden yolda kaldım",
    "servis/araç gelmedi, kendi imkânımla halletmek zorunda kaldım",
    "toplantı beklenenden uzun sürdü, aç kaldım",
    "son dakika görev çıktı, hazırlıksız yakalandım",
    "elimde başka seçenek kalmadığı için cebimden karşıladım",
    "iş yerinde gerekli olan şey yoktu, ben tamamladım",
    "sahada beklenmedik bir durumla karşılaştım",
    "programda olmayan bir aksilik çıktı",
    "kimse yardımcı olamadı, kendim çözmek durumunda kaldım",
]


def _zorunluluk_gerekcesi(baskin_kategori_ham: str) -> str:
    """Kaleme UYAN bir kaçınılmazlık iskeleti. Haritada olmayan kategori genel
    havuza düşer -- genel ifadeler her kalemle tutarlıdır."""
    return random.choice(ZORUNLULUK_GEREKCE.get(baskin_kategori_ham, []) + ZORUNLULUK_GENEL)


# Kategoriye özel havuz bu eşiğe ULAŞIYORSA jenerikler EKLENMEZ (2026-07-30, 2. tur).
# Gerekçe pilottan: `KILIF_GENEL` havuz boyutunu korumak için her kategoriye
# ekleniyordu ama düzeltmeye çalıştığım uyumsuzluğu geri getiriyordu --
#     'Nasip et Kebap'tan ... PERFORMANS TAKİBİ çerçevesinde'
#     'Sultanahmet Coşkun Hotel'den konaklama ... SÜREÇ İYİLEŞTİRME gereğiyle'
#     'Öz Uğur Hipermarketleri'den ... VERİMLİLİK ÇALIŞMASI sürerken'
# Kategoriye özel havuz zaten yeterliyse jeneriğe ihtiyaç yok; yalnız ince
# havuzlarda (ulasim, ofis_sarf, teknoloji gibi 5 girdili) collapse'i önlemek
# için ekleniyor.
KILIF_OZEL_YETERLI_ESIK = 8


def _kilif_havuzu(baskin_kategori_ham: str) -> list[str]:
    """Kategoriye uygun kılıflar; havuz inceyse jeneriklerle desteklenir.
    Haritada olmayan kategori (alkol/eğlence/tütün/kumar gibi yasaklılar) tüm
    havuza düşer -- oralarda zaten `gizleme` dalı devrede ve kılıfı olay
    havuzundan alıyor."""
    ozel = KILIF_KATEGORI.get(baskin_kategori_ham)
    if not ozel:
        return MANIPULATIF_KILIF_HAVUZU
    return ozel if len(ozel) >= KILIF_OZEL_YETERLI_ESIK else ozel + KILIF_GENEL


# Olay benzeri kılıflar hem aidiyet hem eşzamanlılık alabilir (bkz. OLAY_CERCEVELERI).
KILIF_CERCEVELERI = [
    "{kilif} kapsamında", "{kilif} gereği", "{kilif} çerçevesinde", "{kilif} doğrultusunda",
    "{kilif} sırasında", "{kilif} çalışması sürerken", "{kilif} günü",
]
KILIF_CERCEVELERI_KALEM = [
    "{kilif} kapsamında", "{kilif} olarak", "{kilif} çerçevesinde", "{kilif} altında",
]


def kilif_notu_uret(baskin_kategori_ham: str) -> tuple[str, dict]:
    """kurnaz/bariz dalları için şişirilmiş kurumsal kılıf. Her üretimde rastgele --
    sabit literal örnek (eski 'stratejik değerlendirme toplantısı') birebir
    kopyalanıyordu; havuz + çerçeve kombinasyonu aynı yönlendirmeyi tekrar üretmeden verir."""
    if random.random() < 0.25:
        # Gider-kalemi dili kategoriden BAĞIMSIZ kalır: bunlar muhasebe etiketi,
        # hangi kalem olursa olsun aynı şekilde kullanılır ('temsil gideri').
        kilif = random.choice(MANIPULATIF_KILIF_KALEM)
        cerceveli = random.choice(KILIF_CERCEVELERI_KALEM).format(kilif=kilif)
    else:
        # Kategoriye KOŞULLU seçim (bkz. KILIF_KATEGORI). Parametre zaten
        # geliyordu ama kullanılmıyordu -- 'hamburger + yönetim kademesi
        # çalışması' tipi alakasız eşleşmelerin sebebi buydu.
        kilif = random.choice(_kilif_havuzu(baskin_kategori_ham))
        cerceveli = random.choice(KILIF_CERCEVELERI).format(kilif=kilif)
    notu = (f" KILIF: harcamayı '{cerceveli}' gibi büyük bir kurumsal gerekçeye yasla; "
            f"gerçekte sıradan bir alışveriş olduğunu belli etme.")
    return notu, {"kilif": kilif}


# ---------------------------------------------------------------------------
# AYRILMA HÂLİ EKİ -- Python'da hesaplanır, modele HAZIR verilir
# ---------------------------------------------------------------------------
# Türkçede -dan/-den/-tan/-ten tamamen kurallıdır (ünlü uyumu + ünsüz sertleşmesi),
# ama 8B bunu tutturamıyor ("Tanbay Karadeniz'dan" -> doğrusu '-den'). Firma adını
# kullanmayı teşvik ettiğimiz için bu hata neredeyse her açıklamada görünür.
_SESLI = "aeıioöuü"
_KALIN_SESLI = "aıou"
_SERT_SESSIZ = "fstkçşhp"
# İNCE ek alan kalın ünlülü alıntı sözcükler (TDK istisnaları). Firma adlarında
# sık geçenler seçildi: 'Görmeli Seyahat' -> 'Seyahat'ten' (kural '-tan' derdi).
_INCE_EK_ISTISNALARI = {
    "saat", "seyahat", "kalp", "rol", "gol", "usul", "hal", "harf", "sual", "meal",
    "misal", "ihtimal", "mahal", "alkol", "petrol", "kontrol", "konsol", "protokol",
    "sembol", "mentol", "istikbal", "sanayii",
}


# Iyelik ekiyle biten unvanda kaynastirma 'n'si zorunlu: Otel-i-'n-den.
# Olculdu: "Oteli'den" 504, dogrusu 8 -> kural bu vakayi hic tanimiyordu.
# Son ek kurali DEGIL kuratorlu liste: 'taksi', 'teknoloji', 'bayi' de i/u ile
# biter ama iyelik degildir ("Taksi'den" dogrudur). Kumede olmayan kelimede
# davranis eskisiyle birebir ayni ('bayi' hayir, 'bayii' evet).
_IYELIK_SONU = {
    "oteli", "lokantasi", "lokantası", "kuaforu", "kuaförü", "salonu", "berberi",
    "evi", "konukevi", "pansiyonu", "merkezi", "subesi", "şubesi", "yeri",
    "bakkaliyesi", "magazasi", "mağazası", "marketleri", "hipermarketleri",
    "pazari", "pazarı", "carsisi", "çarşısı", "duragi", "durağı", "ciftligi",
    "çiftliği", "mutfagi", "mutfağı", "sofrasi", "sofrası", "ocakbasi",
    "ocakbaşı", "koftecisi", "köftecisi", "borekcisi", "börekçisi", "balikcisi",
    "balıkçısı", "dunyasi", "dünyası", "bayii", "oglu", "oğlu",
    "malzemeleri", "urunleri", "ürünleri", "yemekleri", "hizmetleri",
    "sistemleri", "teknolojileri", "mobilyalari", "mobilyaları", "servisi",
    "sti", "şti",
}


def _iyelik_sonu_mu(kelime: str) -> bool:
    """Son kelime iyelik ekiyle mi bitiyor (kaynaştırma 'n'si gerekir mi)?

    `-evi` BİRLEŞİKLERİ tek tek listelenmez: 'ev' + iyelik kalıbı üretkendir
    (kitabevi, orduevi, öğretmenevi, modaevi, aşevi, konukevi...). Registry'de
    son kelimesi `-evi` ile biten 13 farklı biçimin TAMAMI bu kalıptandır
    (ölçüldü), tek harflik yanlış eşleşmeyi önlemek için asgari uzunluk aranır.
    Bu, `_IYELIK_SONU`'nun küratörlü mantığına DAR bir istisnadır; başka ek için
    genelleme YAPMA (bkz. 'bayi' vs 'bayii')."""
    k = re.sub(r"[^a-zçğıöşü]", "", (kelime or "").lower())
    return k in _IYELIK_SONU or (len(k) > 4 and k.endswith("evi"))


def ayrilma_eki(ad: str) -> str:
    """'Duru Market' -> \"Duru Market'ten\". Özel ada kesme işaretiyle bağlanır.

    İyelik ekiyle biten unvanlarda kaynaştırma 'n'si eklenir
    ('Fırat Oteli' -> "Fırat Oteli'nden"); bkz. `_IYELIK_SONU`."""
    cekirdek = (ad or "").strip().rstrip(".").strip()
    harfler = [h for h in cekirdek.lower() if h.isalpha()]
    if not harfler:
        return cekirdek
    son_kelime = re.sub(r"[^a-zçğıöşü]", "", cekirdek.lower().split()[-1])
    if _iyelik_sonu_mu(son_kelime):
        # Kaynastirma 'n'sinden sonra ses her zaman YUMUSAKTIR (-nden/-ndan);
        # sert sessiz kurali burada UYGULANMAZ.
        son_unlu = next((h for h in reversed(harfler) if h in _SESLI), "a")
        return f"{cekirdek}'n{'da' if son_unlu in _KALIN_SESLI else 'de'}n"
    if son_kelime in _INCE_EK_ISTISNALARI:
        kalin = False
    else:
        son_unlu = next((h for h in reversed(harfler) if h in _SESLI), "a")
        kalin = son_unlu in _KALIN_SESLI
    ek = ("t" if harfler[-1] in _SERT_SESSIZ else "d") + ("a" if kalin else "e") + "n"
    return f"{cekirdek}'{ek}"

# Aynı grup anahtarlarıyla BİREYSEL karşılıklar -> seçim mantığı değişmez.
BIREYSEL_OLAY = {
    "yemek":      ["tek başıma öğle arası", "sahada öğle molası", "mesaiye kalınca akşam yemeği",
                   "yol üstünde hızlı bir şeyler", "eğitim günü öğle arası",
                   "erken vardiya öncesi kahvaltı", "müşteri beklerken ara öğün"],
    "ulasim":     ["görüşmeye yetişme", "işe geliş-gidiş", "otoparka bırakma", "uzun yol görevi",
                   "randevu dönüşü", "gece geç çıkışta dönüş", "servis kaçırma",
                   "yağmurda saha noktasına gidiş"],
    "konaklama":  ["tek kişilik görev seyahati", "sabah erken toplantı için gece kalma",
                   "eğitim programı konaklaması", "uçuş iptali nedeniyle mecburi kalış"],
    "ofis":       ["kendi masamın düzeni", "masamdaki sarfın bitmesi",
                   "evden çalışma günü", "kendi dosyalarımı düzenleme"],
    "teknoloji":  ["kendi cihazımın arızalanması", "kurulum eksiği", "sunum hazırlığı",
                   "ekipman kaybı", "uzaktan bağlantı sorunu"],
    "hizmet":     ["kendi süreçlerim için danışmanlık", "sertifika/eğitim katılımı",
                   "mesleki yetkinlik yenileme"],
    "giyim":      ["saha görevi hazırlığı", "müşteri ziyareti öncesi hazırlık",
                   "hava koşullarının değişmesi"],
    "bakim":      ["seyahat sırasında kişisel ihtiyaç", "sahada hijyen ihtiyacı",
                   "uzun görev öncesi hazırlık"],
    "temizlik":   ["kendi çalışma alanımın temizliği", "araç içi temizlik",
                   "saha dönüşü ekipman temizliği"],
    "genel":      ["kendi görev hazırlığım", "görev sırasında çıkan ihtiyaç",
                   "plan dışı görev"],
}

# Departmana ÖZGÜ ekip olayları; yalnız uygun kalem gruplarında devreye girer.
DEPARTMAN_OLAY: dict[str, list[tuple[str, set[str]]]] = {
    "İnsan Kaynakları": [("mülakat ikramı", {"yemek"}),
                         ("işe alım görüşmeleri", {"yemek", "ulasim"}),
                         ("oryantasyon programı", {"ofis", "teknoloji", "yemek"}),
                         ("çalışan bağlılığı etkinliği", {"yemek", "genel"})],
    "Ar-Ge":            [("sprint değerlendirme toplantısı", {"yemek", "ofis"}),
                         ("prototip çalışması", {"teknoloji", "ofis"}),
                         ("geliştirme ortamı kurulumu", {"teknoloji"})],
    "Satış":            [("bayi ziyareti", {"ulasim", "konaklama", "yemek"}),
                         ("müşteri sunumu", {"yemek", "ofis", "teknoloji"}),
                         ("teklif görüşmesi", {"yemek", "ulasim"})],
    "Pazarlama":        [("fuar standı hazırlığı", {"giyim", "ofis", "konaklama"}),
                         ("lansman etkinliği", {"yemek", "genel"}),
                         ("kampanya çekimi", {"teknoloji", "giyim"})],
    "Finans":           [("ay sonu kapanışı", {"ofis", "yemek"}),
                         ("denetim hazırlığı", {"ofis", "hizmet"}),
                         ("mali müşavirlik görüşmesi", {"hizmet", "yemek"})],
    "Lojistik":         [("sevkiyat planlaması", {"ulasim", "hizmet"}),
                         ("depo sayımı", {"ofis", "yemek"}),
                         ("araç filosu ihtiyacı", {"ulasim"})],
    "Operasyon":        [("vardiya devri", {"yemek", "ofis"}),
                         ("süreç denetimi", {"hizmet", "ofis"}),
                         ("saha ekibi desteği", {"yemek", "ulasim", "bakim"})],
    "Proje Yönetimi":   [("proje başlangıç toplantısı", {"yemek", "ofis"}),
                         ("müşteri statü toplantısı", {"yemek", "ulasim"}),
                         ("teslim öncesi hazırlık", {"ofis", "teknoloji"})],
    "İdari İşler":      [("misafir ağırlama", {"yemek", "bakim"}),
                         ("ofis düzeni", {"ofis", "temizlik"}),
                         ("toplantı organizasyonu", {"yemek", "ofis", "genel"})],
    "Saha Operasyon":   [("kurulum ziyareti", {"ulasim", "konaklama", "teknoloji"}),
                         ("arıza müdahalesi", {"teknoloji", "ulasim"}),
                         ("periyodik bakım", {"teknoloji", "temizlik"})],
}


def olay_sec(departman: str | None, baskin_kategori_ham: str) -> tuple[str, str]:
    """(olcek, olay) döner. Departmana özgü olaylara ağırlık verir ama genel havuzu
    kapatmaz -- her departman her olayı yaşayabilir, kapatmak çeşitliliği kısardı."""
    grup = KATEGORI_GRUBU.get(baskin_kategori_ham, "genel")
    olcek = random.choices(("bireysel", "ekip"),
                           weights=OLCEK_AGIRLIK.get(grup, (0.4, 0.6)))[0]
    if olcek == "bireysel":
        return olcek, random.choice(BIREYSEL_OLAY.get(grup, BIREYSEL_OLAY["genel"]))
    ozel = [o for o, gruplar in DEPARTMAN_OLAY.get(departman or "", []) if grup in gruplar]
    genel = GRUP_OLAY.get(grup, GRUP_OLAY["genel"])
    return olcek, random.choice(ozel * 2 + genel if ozel else genel)


# Olay (BAĞLAM) enjeksiyon olasılığı.
#
# DENENDİ VE GERİ ALINDI (2026-07-30): `yeterli` için %50 -> %100 çıkarıldı,
# hipotez "bağlam verilmeyen yarıda model amacı icat edemeyip KALEMİ TEKRAR
# EDİYOR" idi ('Müşteri konaklaması için pansiyon konaklama aldım'). Aynı 200
# kayıtta tek değişken olarak ölçüldü:
#     totoloji  %10,2 -> %11,2   (DEĞİŞMEDİ)
#     dist-1    0,486 -> 0,473   (düştü)
# Hipotez YANLIŞ çıktı: olay havuzundan kök taşıyan `yeterli` çıktıları enjeksiyon
# %50'yken ZATEN %89,8'di. Model bağlamdan yoksun değil; kusur, amacın bazen
# kalemin YANKISI olması ve daha çok bağlam vermek yankıyı durdurmuyor
# ('Eğitim programı konaklaması için ... konaklama ücreti' -- gerçek olay VAR,
# tekrar yine var).
#
# %50 KASITLI: her `yeterli` bağlam cümlesiyle gelirse şablon imzası doğar ve
# aşağı akıştaki model kategoriyi içerikten değil kalıptan öğrenir (leakage).
# Doğru kaldıraç bağlam ORANI değil, amacın kalemi tekrarlamasını doğrudan
# yasaklayan talimat (bkz. `yeterli` talimatındaki "fişe bakarak anlaşılamayacak"
# kuralı).
OLAY_OLASILIGI = 0.5


# --- OLAY TÜRÜ: SÜRE mi NEDEN mi? (2026-08-01) ------------------------------
#
# OLAY_CERCEVELERI'nin 10 kalıbından 7'si SÜRE bildirir ('sırasında', 'devam
# ederken', 'günü', 'sonrasında'...) ve `bireysel` dalı zaten SABİT olarak
# "'{olay}' sırasında" yazıyordu. Havuzda ise ANLIK/TETİKLEYİCİ olaylar var --
# bunlara süre kalıbı takılınca Türkçe bozuluyor:
#     "servis kaçırma sırasında ... otopark ücreti ödedim"      (ölçüldü: 83 metin,
#     "kendi cihazımın arızalanması sırasında"                   %76'sında kalem de
#     "hava koşullarının değişmesi sırasında"                    uyumsuzdu)
# Servisi kaçırmak bir SÜRE değil, bir NEDENdir.
#
# Çözüm: bu olayların NEDEN biçimini hazır yazıyoruz (tıpkı `ayrilma_eki`'nde
# -dan/-den'i modele bırakmayıp hesapladığımız gibi). Değerler TAM ifadedir,
# üzerine çerçeve UYGULANMAZ -- '-dığı için' çekimini 8B'ye kurdurmak yerine
# doğrusunu veriyoruz. Varyant havuzu sabit kalıp collapse'ını da önler.
OLAY_NEDEN_BICIMI: dict[str, list[str]] = {
    "servis kaçırma": ["servisi kaçırdığım için", "servise yetişemediğimden",
                       "servis saatini kaçırdığımdan ötürü"],
    "kendi cihazımın arızalanması": ["kendi cihazım arızalandığı için",
                                     "cihazım bozulduğundan"],
    "ekipman kaybı": ["ekipmanımı kaybettiğim için", "ekipman kaybolduğundan"],
    "masamdaki sarfın bitmesi": ["masamdaki sarf bittiği için",
                                 "masamdaki malzeme tükendiğinden"],
    "hava koşullarının değişmesi": ["hava koşulları değiştiği için",
                                    "hava aniden bozduğundan"],
    "uzaktan bağlantı sorunu": ["uzaktan bağlantı sorunu yaşadığım için"],
    "kurulum eksiği": ["kurulum eksik kaldığı için"],
    "otoparka bırakma": ["aracı otoparka bırakmam gerektiği için"],
    "görüşmeye yetişme": ["görüşmeye yetişmem gerektiği için"],
    "plan dışı görev": ["plan dışı bir görev çıktığı için"],
    "görev sırasında çıkan ihtiyaç": ["görev sırasında ihtiyaç çıktığı için"],
}

# Kendi bağlacını ZATEN taşıyan olaylar: çerçeve eklenirse çift bağlaç olur
# ('uçuş iptali nedeniyle mecburi kalış sırasında').
OLAY_CERCEVESIZ = {"uçuş iptali nedeniyle mecburi kalış"}


def olay_cercevele(olay: str) -> str:
    """Olayı cümleye gömülecek biçime sokar: NEDEN olayları hazır çekimli
    döner, SÜRE olayları rastgele bir OLAY_CERCEVELERI kalıbına girer."""
    neden = OLAY_NEDEN_BICIMI.get(olay)
    if neden:
        return random.choice(neden)
    if olay in OLAY_CERCEVESIZ:
        return olay
    return random.choice(OLAY_CERCEVELERI).format(olay=olay)


def baglam_notu_uret(persona: dict, baskin_ham: str, manipulatif_dal: str | None = None) -> tuple[str, dict]:
    """Prompt'a eklenecek BAĞLAM cümlesi + meta. Boş string dönebilir (bilinçli).

    ~%50 olasılık KASITLI: her `yeterli` bağlam cümlesiyle başlarsa şablon imzası
    doğar ve aşağı akıştaki model kategoriyi içerikten değil kalıptan öğrenir
    (leakage). %100 denendi ve ÖLÇÜMLE geri alındı -- bkz. OLAY_OLASILIGI.

    manipulatif_dal verilirse yalnız 'gizleme'/'zorunluluk' dallarına bağlam verilir --
    o faturalar ANOMALİLİ olduğu için `onay_durumu` metne bakmadan zaten 'onaylanmadi';
    metnin meşru görünmesi etiketi bozmaz, aksine örneği gerçekçi kılar. 'bariz'/'kurnaz'
    ağırlıkla TEMİZ faturalarda çalışır ve orada etiket TAMAMEN metinden gelir; sağlam bir
    iş bağlamı vermek onları `yeterli`den ayırt edilemez kılıp etiketi gürültüye çevirirdi.
    """
    if manipulatif_dal is not None and manipulatif_dal not in ("gizleme", "zorunluluk"):
        return "", {}
    if random.random() >= OLAY_OLASILIGI:
        return "", {}
    dep = ROL_DEPARTMAN.get(persona.get("rol", ""))
    if not dep:
        return "", {}
    olcek, olay = olay_sec(dep, baskin_ham)
    cerceveli = olay_cercevele(olay)
    meta = {"departman": dep, "olay": olay, "olcek": olcek}

    if manipulatif_dal == "gizleme":
        # Olay ÖRTÜNÜN kendisi: somut bir bahane, kurumsal bulamaç değil.
        notu = (f" BAĞLAM: {dep} birimindesin; anlatacağın kılıf '{olay}'. Bunu kendi "
                f"cümlenle, doğal biçimde kur.")
    elif manipulatif_dal == "zorunluluk":
        notu = (f" BAĞLAM: {dep} birimindesin ve harcama {cerceveli} oldu; "
                f"kaçınılmazlığı BU duruma yasla, havada bırakma.")
    # OLAY cümlenin BAŞINDA: departman öne alındığında model onu AMAÇ sanıyordu
    # ("Ar-Ge için kisisel bakim ürünleri aldım"). Departman artık parantezde, ikincil.
    elif olcek == "bireysel":
        # 'sırasında' SABİTİ KALDIRILDI (2026-08-01): anlık olaylara takılınca
        # "servis kaçırma sırasında" gibi bozuk Türkçe üretiyordu. Artık çerçeveyi
        # olayın türü belirler (bkz. olay_cercevele). Olay yine cümlenin BAŞINDA --
        # departman öne alınınca model onu AMAÇ sanıyordu (aşağıdaki nota bak).
        notu = (f" BAĞLAM: {cerceveli} bu harcamayı KENDİN için tek başına yaptın "
                f"({dep} birimindesin). Bu senin kendi masrafın; bağlamı kendi cümlenle anlat.")
    else:
        notu = (f" BAĞLAM: bu harcama {cerceveli} yapıldı ({dep} birimindesin). "
                f"Bunu kendi cümlenle anlat.")
    return notu, meta


def persona_metni(p: dict) -> str:
    return (f"{p['kidem']} bir {p['rol']}sın, şu an {p['ruh']} bir ruh halindesin; "
            f"yazım tarzın: {p['yazim']}")

# Kategoriye özel örnekleme sıcaklığı. yeterli fişteki gerçek kalemlere
# dayanmalı -> düşük temp uydurma/halüsinasyonu azaltır. Diğerleri çeşitlilik için
# 1.1'e çekildi: min_p (0.1) güvenlik ağı sayesinde yüksek sıcaklıkta bile
# tutarlılık korunur, çeşitlilik artar (bkz. min-p sampling, arXiv:2407.01082).
KATEGORI_SICAKLIK = {
    "yeterli": 0.6,
    "yetersiz": 1.1,
    "manipulatif": 1.1,
    "ai_uretimi": 1.1,
}

# Sıcaklık TAVANI (None = kapalı, tablo aynen kullanılır).
#
# NEDEN VAR: yukarıdaki 1.1 değerleri Ollama'da `min_p=0.1` GÜVENLİK AĞIYLA
# BİRLİKTE kalibre edildi -- min_p yüksek sıcaklıkta aday kümesinin şişmesini
# engelliyor (bkz. ollama_cagir yorumu, arXiv:2407.01082). OpenAI-uyumlu
# arayüzde (Groq) `min_p` YOKTUR, yani bulut yolunda 1.1 ÇIPLAK çalışıyor.
# Llama 3.3 pilotunda bunun izleri görüldü (yersiz -mIş kipi, firma_ek_hatasi).
#
# Tavan yalnız YÜKSEK olanları kırpar; `yeterli` 0.6 zaten altında kalır, yani
# tek değişken izole edilmiş olur (A/B testi için).
SICAKLIK_TAVANI: float | None = None

# yeterli talimatindaki SADELESTIRME ORNEGI verilsin mi (bkz. prompt_olustur).
SADE_ORNEK_VER: bool = True


def baskin_kategori(kalemler: list[dict]) -> str:
    """Fişteki kalemlerin en sık geçen harcama kategorisi (baskın tema).
    yeterli prompt'unu çıpalamak için kullanılır."""
    sayac = Counter(k["harcama_kategorisi"] for k in kalemler)
    return sayac.most_common(1)[0][0]


def prompt_olustur(fatura: dict, kategori: str, anomali_turleri: list[str] | None = None) -> tuple[str, str, dict]:
    """
    (system_prompt, user_prompt, meta) döndürür. `meta`, kategoriye özel
    beklentiyi taşır (ai_uretimi için seçilen `kapanis`, manipulatif için
    `gizlenecek` kalem) -- böylece ihlalleri_bul() üretilen metni gerçek
    beklentiye karşı denetleyebilir.

    `anomali_turleri`: faturanın etiketindeki anomali listesi. manipulatif
    dalının hangi alt-üslubu seçeceğini belirler (bkz. aşağıdaki yorum).
    """
    anomali_turleri = anomali_turleri or []
    kalem_ozeti = kalemler_ozetle_prompt(fatura["kalemler"])  # insan-okur (enum sızıntısını keser)
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    meta: dict = {}
    # Persona kategori dallarından ÖNCE üretilir: departman ondan türetiliyor
    # (bkz. baglam_notu_uret) ve `yeterli`/`manipulatif` talimatına giriyor.
    persona = persona_uret()
    baskin_ham = baskin_kategori(fatura["kalemler"])   # ham enum (KATEGORI_GRUBU anahtarı)
    firma_ekli = ayrilma_eki(firma_kisa)               # "Duru Market'ten" -- hazır çekim
    yuklem = yuklem_ipucu(baskin_ham)                  # 'aldım' tekelini kırar

    # Kategoriden bağımsız uzunluk hedefi seç; hem prompt'a yaz hem meta'ya koy
    # (ihlalleri_bul uzunluk denetimini bu hedefe göre yapsın -- kategori-uzunluk
    # sahte korelasyonunu kırar).
    uz_ad, uz_tarif, uz_alt, uz_ust = uzunluk_hedefi_sec(kategori)
    meta["uzunluk"] = (uz_ad, uz_alt, uz_ust)

    # SISTEM PROMPTU (Sabit -> Ollama önbelleğe alır). whaatif.py'nin karakter-
    # daldırma çerçevesi esas alındı: dört karakterin KONTRASTLI tanımı, modele her
    # kategorinin sesini VE diğerlerinden farkını öğretir (kural listesi tek başına
    # bunu vermiyordu). Bizim kurallarımız (fişteki kalemler, rakam yok, pasif kalıp
    # yasağı) korunur. Kategoriye özel ayrıntı user prompt'taki 'Talimat'ta.
    system_prompt = (
        # "Sen bir yapay zeka DEĞİLSİN. 
        "Sen, ay sonu geldiği için masraf sistemine fiş giren "
        "gerçek bir şirket çalışanısın. Sana verilen KARAKTER tipine tam olarak bürüneceksin; "
        "bu dört karakter birbirinden kesinlikle farklı insanlar gibi konuşur:\n"
        "1) YETERLİ çalışan → işini düzgün yapan, ne için harcadığını rahatça söyleyen, kendinden emin biri.\n"
        "2) YETERSİZ çalışan → tembel, aceleci, detay vermeyi dert etmeyen, umursamaz biri (yalan söylemez, sadece anlatmaya üşenir).\n"
        "3) MANİPÜLATİF çalışan → bir şeyi bilerek gizleyen ya da abartıyla haklı çıkaran, sahte kurumsal gerekçe uyduran kurnaz biri.\n"
        "4) AI_URETIMI → tek istisna: insan değil, resmi ve robotik bir yapay zeka gibi yazacaksın.\n"
        "Sana hangi karakter verildiyse SADECE onun gibi yaz; diğer üç karakterin üslubuna asla kayma "
        "(yetersiz asla amaç açıklamaz, yeterli asla amacı gizlemez, manipülatif asla gerçek/aykırı kalemi açıkça söylemez).\n"
        "KURALLAR:\n"
        # "Yalnızca fişte bulunan kalemlerden söz et" kuralı, gizlenecek kalemi hiç
        # anmaması gereken MANİPÜLATİF ve kalemden hiç bahsetmeyen YETERSİZ talimatıyla
        # çelişiyordu -> 8B çelişkili talimatta ortalama alıp karakteri bozuyordu.
        # Kural artık "ne yapma"ya değil UYDURMAYA odaklı: kalem anmamak serbest.
        "- Fişte OLMAYAN bir ürün/hizmet aldığını söyleme (uydurma).\n"
        "- (AI_URETIMI hariç) tutar/rakam yazma; fiyat, toplam, adet gibi sayılar açıklamada geçmesin.\n"
        "- Açıklama HARİCİNDE hiçbir şey yazma ('İşte açıklama:', 'Tabii', 'Açıklama:' YASAK).\n"
        "- Tamamen Türkçe konuş; İngilizce kelime kullanma.\n"
        "- (AI_URETIMI hariç) edilgen/resmi kalıplar (edildi, edilmiştir, sağlanmıştır, karşılanmıştır) KULLANMA; "
        "birinci tekil şahıs, doğal ve konuşma diline yakın yaz.\n"
        "- İki karaktere birden benzeyen belirsiz cümle kurma; net bir karaktere gir.\n"
        # Eskiden koşulsuz "firma adını kullan" deniyordu; ai_uretimi ("ASLA kullanma")
        # ve yetersiz ("istersen kullanma") talimatlarıyla çelişiyordu. Kural artık
        # KULLANIP KULLANMAMAYI değil, kullanılırsa BİÇİMİNİ düzenliyor.
        "- Firma/satıcı adını KULLANACAKSAN kaynak olarak kullan, doğru '-dan/-den/-tan/-ten' ekiyle: "
        "'ABC Yazılım'dan lisans aldık', 'XYZ Market'ten malzeme aldım' gibi.\n"
    )

    if kategori == "yeterli":
        uslup = random.choice(YETERLI_USLUP_IPUCLARI)
        baskin = baskin_ham.replace("_", " ")
        # YAPI iskelesi: Türkçenin amaç-sonuç / neden-sonuç kalıbını hatırlatıp
        # mantıklı senaryo kurdurur -- `yeterli`nin ana çeşitlilik kaldıracı.
        iskele = random.choice(YETERLI_ISKELE).format(firma=firma_kisa)
        # Sadeleştirme örneği faturanın KENDİ kaleminden kurulur (sabit örnek
        # veriliyorken 8B onu alakasız fişlerin metnine taşıyordu).
        ham_ad = max((k["aciklama"] for k in fatura["kalemler"]), key=len, default="")
        if SADE_ORNEK_VER:
            sade_ad = kalem_adi_sadelestir(ham_ad)
            sade_ornek = (f"('{ham_ad}' -> '{sade_ad}' gibi)"
                          if sade_ad and sade_ad != ham_ad.lower()
                          else "('F Saff Sıvı El Sabunu 500Ml' -> 'el sabunu' gibi)")
        else:
            # ÖRNEKSİZ mod: sadeleştirmeyi modelin kendisi yapar.
            #
            # NEDEN GEREKTİ: `kalem_adi_sadelestir` market tipi adlarda SON iki
            # kelimeyi alıyor, ama o adlarda son kısım KOKU/VARYANT oluyor
            # ('... Sabun 5'li ÇİÇEK BUKETİ 375Gr' -> 'cicek buketi'). Örnek
            # talimata birebir girdiği için model yanlış kelimeyi ÖĞRENİYORDU --
            # pilotta "çiçek aldım" çıktısının kaynağı buydu (ölçüldü: 20k
            # faturada kalemlerin %3,6'sı koku/varyant ile biten sadeleştirme
            # üretiyor). 8B'de bu koltuk değneği gerekliydi; daha büyük bir
            # modelde yükten ibaret olabilir -- bu bayrak onu ölçmek için.
            sade_ornek = "(marka/ölçü/koku detayını atıp ürünün kendisini yaz)"
        talimat = (
            f"KARAKTER: YETERLİ çalışan. Harcamanın İŞ AMACINI (kiminle, ne için) net ve kendinden emin "
            f"söyle - saklayacak bir şeyin yok. YAPI: {iskele} "
            f"Fişteki gerçek kalemi ÇALIŞAN gibi SADELEŞTİREREK an {sade_ornek}; "
            # PEMBE FİL vs YÜK TAŞIYAN YASAK (2026-07-30, İKİ TURDA ölçüldü):
            # 1. tur: buradaki yasak kategori adını GÖSTERİYORDU ('{baskin}').
            #    Pembe fil sanıp "ürünün TÜRÜNÜ amaç yerine koyma"ya çevirdim.
            # 2. tur (pilot): enum_sizinti `yeterli`de %3,0 -> %10,2'ye ÇIKTI.
            #    "tür" kelimesi modele "sistem kategori etiketi"ni anlatmıyormuş;
            #    açık yasak YÜK TAŞIYORMUŞ.
            # Sonuç: yasak AÇIK haliyle geri, ama literal kategori adı GÖSTERİLMEDEN
            # -- ilkenin evrensel olmadığı, kısıtın NET olması gerektiği ders.
            f"boyut/miktar/model detayı yazma. Amaç olarak sistemin sınıflandırma "
            f"etiketini yazma (masraf sistemindeki kategori adları); gerçek sebebi söyle. "
            # AMAÇ TOTOLOJİSİ (2026-07-30, ölçülen %10,2): model amaç yerine kalemin
            # adını tekrarlıyordu -- 'Müşteri KONAKLAMASI için pansiyon KONAKLAMA
            # aldım', 'Havalimanı TRANSFERİ için ... TRANSFER hizmeti'. Bağlam
            # enjeksiyonunu %100'e çıkarmak İŞE YARAMADI (bkz. OLAY_OLASILIGI):
            # model bağlamdan yoksun değil, yankıyı üstüne ekliyor. Bu yüzden
            # kısıt DOĞRUDAN veriliyor. Ölçüt `yeterli`nin tanımıyla aynı: fişte
            # zaten GÖRÜNEN (firma, kalem) değil GÖRÜNMEYEN (neden) anlatılmalı.
            f"AMAÇ, fişe bakarak anlaşılamayacak bir bilgi olmalı: aldığın şeyin "
            f"adını ya da türünü sebep diye gösterme, onu neyin gerektirdiğini söyle. "
            f"Birinci tekil şahıs ve GEÇMİŞ zaman (harcama olmuş bitmiş -- 'alacağım' değil 'aldım'); "
            f"edilgen ya da 3. şahıs ('alındı', 'aldı') KULLANMA. Üslup: {uslup}. "
            f"Bu bir YETERSİZ not DEĞİL: amacı belirsiz bırakma. Bu bir MANİPÜLATİF not da DEĞİL: gerçek "
            # Firma adı ÇEKİMLENMİŞ hâliyle veriliyor (ayrilma_eki): ek Türkçede kurallı
            # ama 8B tutturamıyor ('Karadeniz'dan'). Modelin çekim yapması gerekmesin.
            f"amacı savunmaya geçmeden söyle. Firma: {firma_kisa} (kaynak olarak: {firma_ekli}). "
            f"Yüklem olarak '{yuklem}' gibi kaleme uyan doğal bir fiil kullan."
        )
        _baglam, _bmeta = baglam_notu_uret(persona, baskin_ham)
        talimat += _baglam
        meta.update(_bmeta)

    elif kategori == "yetersiz":
        uslup = random.choice(YETERSIZ_USLUP_IPUCLARI)
        # PEMBE FİL DÜZELTMESİ (2026-07-30): burada eskiden kategori adı
        # GÖSTERİLİP yasaklanıyordu ("Kategori adını ('ofis mobilya') olduğu gibi
        # yazma") -- yasaklanan ifadeyi adıyla anmak onu ÜRETTİRİYOR
        # (docs/arsiv/faz-b-prompt.md §6, ölçülmüş). Yön artık POZİTİF: yasak dize hiç
        # gösterilmez, yerine günlük dildeki genel ad ÖNERİLİR.
        #
        # COLLAPSE RİSKİ DÜŞÜNÜLDÜ: genel ad SABİT bir kalıp değil, faturanın
        # kategorisine göre 13 farklı değer alıyor -- §8'in "sabit kalıp collapse
        # üretir, havuz üretmez" ilkesine uygun. Ayrıca zorunlu değil, talimattaki
        # üç seçenekten (kuru öbek / umursamaz söz / genel ad) biri.
        _genel_ad = _KATEGORI_GENEL_AD.get(baskin_kategori(fatura["kalemler"]))
        genel_ipucu = f" (ör. '{_genel_ad}')" if _genel_ad else ""
        # _dizgi_carpit: havuzun 61 girdisi de kucuk harf + noktasiz; oldugu gibi
        # gosterilince `yetersiz` ciktilarinin %100'u oyle cikiyor ve kategori
        # icerige bakmadan bilinebilir hale geliyordu (bkz. _DIZGI_CEVIRME_ORANI).
        ornekler = ", ".join(f"'{_dizgi_carpit(o)}'"
                             for o in random.sample(YETERSIZ_ORNEK_HAVUZ, 2))
        talimat = (
            f"KARAKTER: YETERSİZ çalışan. Baştan savma, muğlak, geçiştirmelik bir not yaz. İşi kiminle/neden "
            f"yaptığını ASLA söyleme - ama gizlemeye de uğraşma, sadece yazmaya üşen. Üslup: {uslup}. "
            f"Kuru bir öbek ('genel gider') ya da umursamaz bir söz ('gerekliydi aldım işte') olabilir, ör. {ornekler} "
            f"(birebir kopyalama, tarzını yakala). FİİL kullanacaksan BİRİNCİ TEKİL ŞAHIS kullan "
            f"('aldım', 'ödedim'); 'alındı', 'aldılar', 'karşılandı' gibi edilgen ya da 3. şahıs YAZMA "
            f"(üşengeçsin ama notu sen yazıyorsun). İstersen hiç fiil kullanma, kuru bir öbek bırak. "
            # 'nereye/hangi birime alındığını yazma' EKLENDI ve GERI ALINDI: (a) hemen
            # asagidaki "Firma: ... (istersen kullanma)" ile CELISIYORDU (§6: celiskili
            # talimatta 8B ortalama alip karakteri bozuyor), (b) 'birim' kavramini
            # prompt'a sokmak yeni bir pembe fil. Amac zaten karsilaniyor: talimatin
            # basindaki "kiminle/neden ASLA soyleme" amaci gizliyor, genel ad onerisi
            # de 'ofis mobilya'daki NEREYE bilgisini dusuruyor.
            f"Ürün türünü anacaksan sınıflandırma etiketi gibi değil, günlük dilde "
            f"nasıl söylüyorsan öyle an{genel_ipucu}. "
            f"Bu bir MANİPÜLATİF not DEĞİL: kurnaz gerekçe uydurma, sahte "
            f"kılıf yok. Firma: {firma_kisa} (istersen kullanma)."
        )
    elif kategori == "manipulatif":
        # DAL SEÇİMİ ANOMALİ-FARKINDALI: çalışan neyi örttüğünü BİLİR, dolayısıyla
        # ürettiği metnin üslubu anomali türüyle nedensel olarak bağlıdır. Eskiden
        # yalnız gizlenecek KALEM aranıyor, bulunmazsa bariz/kurnaz yazı-turası
        # atılıyordu -- oysa limit_asimi/fatura_no_tekrari/gelecek_tarihli de A-grubu
        # (bilinçli) anomaliler; onlarda da örtülen somut bir şey var, sadece "kalem"
        # değil. Eşleme:
        #   yasakli_kategori | is_kolu_kategori_uyumsuzlugu -> GİZLEME  (somut kalem)
        #   limit_asimi                                     -> ZORUNLULUK (tutarı anmadan kaçınılmazlık)
        #   mukerrer_fis_yukleme                            -> KURNAZ  (aynı fişi ikinci kez yüklemek
        #                                                      bilinçli bir kurnazlıktır -> metin dikkat
        #                                                      çekmemeli, rutin görünmeli)
        #   temiz / gelecek_tarihli / fatura_no_cakismasi /  -> BARİZ | KURNAZ 50/50
#   salt teknik anomali
        # gelecek_tarihli BİLEREK dışarıda: çalışan gelecek tarihli bir fişi bilerek
        # yüklemez (böyle bir fiş zaten olamaz) -> örtülecek bir kurnazlık yok, dal
        # seçimi genel yazı-turasına kalır.
        #
        # NOT: `yasakli_kategori` buraya ETİKET ADIYLA değil, KALEMİN KENDİSİYLE giriyor --
        # yasakli_kalem_bul() alkol/eğlence/tütün/kumar kategorili kalemi bulur ve gizleme
        # dalını açar (o kalemden hiç bahsetmeyip "araya kaynatma" davranışı). Etiket yerine
        # kalemi aramak daha sağlam: union etiketi eksik kalsa bile gizleme dalı çalışır.
        yasakli = yasakli_kalem_bul(fatura["kalemler"])
        if yasakli:
            dal, gizlenecek = "gizleme", yasakli
        elif "is_kolu_kategori_uyumsuzlugu" in anomali_turleri:
            dal, gizlenecek = "gizleme", aykiri_kalem_bul(fatura["kalemler"])
        elif "limit_asimi" in anomali_turleri:
            # MAĞDURİYET, zorunluluk'un KARDEŞ dalı: ikisi de kaçınılmazlık kurar
            # ama öznesi farklı (iş uzadı ↔ ben mahsur kaldım). %30'u mağduriyete
            # ayrılıyor -- zorunluluk baskın kalsın, ama tek tip olmasın.
            dal, gizlenecek = ("magduriyet" if random.random() < 0.30 else "zorunluluk"), None
        elif "mukerrer_fis_yukleme" in anomali_turleri:
            dal, gizlenecek = "kurnaz", None
        else:
            # 70/30 bariz lehine (eskiden 50/50): kurnaz dal bilerek "ince çizgi"
            # üretiyor ve pilotta insan gözüyle manipülatif olduğu ayırt edilemiyordu.
            # v1'de önce KALIN çizgi öğrenilsin; ince örnekler gizleme/zorunluluk/
            # kurnaz dallarından yine geliyor.
            dal, gizlenecek = ("bariz" if random.random() < 0.70 else "kurnaz"), None
        # aykiri_kalem_bul None dönebilir (tek kalemli / tek kategorili fiş) -> gizlenecek
        # bir şey yoksa gizleme talimatı kurulamaz, kurnaz dala düş.
        if dal == "gizleme" and not gizlenecek:
            dal = "kurnaz"
        meta["gizlenecek"] = gizlenecek
        meta["dal"] = dal

        # Koşul `dal == "gizleme"` yerine doğrudan `gizlenecek` üzerinden kuruluyor:
        # yukarıdaki fallback'ten sonra ikisi EŞDEĞER (gizleme dalı ⟺ gizlenecek dolu),
        # ama bu biçim tip denetleyicisine de None olmadığını gösterir.
        if gizlenecek:
            gizli_kat = gizlenecek["harcama_kategorisi"].replace("_", " ")
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Bu fişte '{gizlenecek['aciklama']}' ({gizli_kat}) var ve "
                f"şirket bunu ödemez/uygun görmez. Görevin bunu kurnazca GİZLEMEK! 'Müşteri ağırlama', "
                f"'temsil gideri', 'toplantı gideri' gibi aşırı kurumsal kılıflar uydur. Gerçek kalemin adını "
                f"ya da türünü ('{gizli_kat}') hiçbir formda geçirme. Bu bir YETERLİ not DEĞİL: gerçek amacı "
                f"gizliyorsun. Bu bir YETERSİZ not da DEĞİL: kısa geçiştirmiyorsun, iddialı bir kılıf kuruyorsun. "
                f"Kendinden aşırı emin, sahte kurumsal dille yaz ama BİRİNCİ TEKİL ŞAHIS kal: 'alındı', "
                f"'karşılandı', 'gerçekleştirildi' gibi edilgen/3. şahıs kalıp KULLANMA. "
                f"Satıcı: {firma_kisa}."
            )
        elif dal == "magduriyet":
            # MAĞDURİYET dalı: kaçınılmazlık ÇALIŞANIN başına gelenden kurulur
            # (zorunluluk'ta İŞTEN kurulur). Şirketin ödememesi gereken gideri
            # "mağdur oldum, mecbur kaldım" çerçevesine sığdırır.
            # Vurgu YOK: mağduriyet anlatısı ısrarla birleşince bariz dala benzer.
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Bu masraf aslında şirketin karşılaması gereken bir gider "
                f"DEĞİL ve bunu biliyorsun. Ama kendini MAĞDUR gibi anlat: {random.choice(MAGDURIYET_CERCEVELERI)} "
                f"-- bu yüzden bu masrafı yapmak zorunda kaldığını söyle. Tutardan, limitten, kuraldan HİÇ "
                f"söz etme; şikâyet etme, sadece başına geleni sakin sakin anlat ki karşı taraf sana hak versin. "
                f"Bu bir YETERLİ not DEĞİL: gerçek bir iş amacı anlatmıyorsun, kendi mağduriyetini anlatıyorsun. "
                f"Abartılı vurgu ya da savunmacı ısrar KULLANMA. Birinci tekil şahıs ve GEÇMİŞ zaman; "
                f"'kalındı', 'yapıldı' gibi edilgen/3. şahıs kalıp YAZMA ('kaldım', 'yaptım' de). "
                f"Satıcı: {firma_kisa}."
            )
        elif dal == "zorunluluk":
            # ZORUNLULUK dalı (limit_asimi): çalışan masrafın fazla olduğunu BİLİR.
            # Tutarı hiç anmadan kaçınılmazlık kurar. Abartılı vurgu YOK -- olursa
            # bariz dala benzer ve ayrım kaybolur.
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Bu masraf şirketin uygun gördüğünden PAHALIYA geldi ve bunu "
                f"biliyorsun. Tutardan, limitten, fiyattan HİÇ söz etme; bunun yerine harcamayı KAÇINILMAZ "
                # Gerekçe artık KALEME UYAN bir iskeletten geliyor (bkz.
                # ZORUNLULUK_GEREKCE). Eski sabit menü kategoriden bağımsızdı ve
                # ters mantık üretiyordu: 'iş bekleyemezdi' kargo için doğru,
                # KONAKLAMA için saçma ('acil iş var, o yüzden geceledim').
                f"göster; zorunluluğu şuna yasla: {_zorunluluk_gerekcesi(baskin_ham)}. "
                f"Bunu kendi cümlenle kur. Sakin ve kendinden emin yaz; abartılı vurgu ya da savunmacı ısrar "
                f"KULLANMA. Bu bir YETERLİ not DEĞİL: gerçek gerekçeyi değil, kendini aklayan bir zorunluluk "
                f"hikâyesi anlatıyorsun. Birinci tekil şahıs; 'zorunda kalındı', 'alındı' gibi edilgen/3. şahıs "
                f"kalıp YAZMA ('kalmadım', 'aldım' de). "
                f"Kusur/aykırılık/prosedürden bahsetme. Satıcı: {firma_kisa}."
            )
        elif dal == "bariz":
            # BARİZ dal: abartılı savunmacı + zorunlu vurgu (konum meta ile denetlenir).
            vurgu = random.choice(MANIPULATIF_VURGU_IPUCLARI)
            konum = random.choice(MANIPULATIF_VURGU_KONUMLARI)
            meta["vurgu"] = vurgu
            meta["vurgu_konum"] = konum
            talimat = (
                f"KARAKTER: MANİPÜLATİF çalışan. Masraf aslında normal ama sen bunu abartılı ve gereksiz "
                f"savunmacı yaz; sanki suçluluk duyuyormuşsun gibi ikna etmeye çalış. '{vurgu}' gibi abartılı "
                f"bir vurguyu {konum} yerleştir ama KENDİ cümlenle kur, ezber kalıbı ('yüzde yüz işle "
                f"alakalıdır' gibi) aynen tekrarlama. Bu bir YETERLİ not DEĞİL: sakin değilsin, gereğinden "
                f"fazla ısrarcısın. Edilgen ya da 3. şahıs ('alındı', 'kullanıldı') YAZMA; birinci tekil şahıs kal. "
                f"Kusur/aykırılık/prosedürden bahsetme. Sadece açıklama metnini yaz."
            )
        else:
            # KURNAZ dal: abartılı vurgu YOK. İki durumu birden karşılar: (a) fişte
            # dikkat çekmemesi gereken bir şey var (mukerrer_fis_yukleme -- aynı fişi ikinci
            # kez yükleme), (b) masraf sorunsuz ama sıradan alışveriş şişirilmiş kurumsal
            # kılıfla önemli gösteriliyor. Ortak hedef: metin SORGUSUZ onaylansın.
            # Sınırda (yeterliye yakın) -> ayırt etmeyi öğretir.
            talimat = (
                "KARAKTER: MANİPÜLATİF çalışan. Amacın bu masrafın sorgusuzca onaylanması: dikkat çekmeyen, "
                "rutin ve inandırıcı görünen bir gerekçe yaz. Sıradan bir alışverişi ÖNEMLİ bir iş kararıymış "
                # Buradaki üç literal örnek KALDIRILDI: 'stratejik değerlendirme toplantısı'
                # pilotlarda birebir kopyalanıyordu (§15'te ölçüldü, qwen3_3'te hâlâ çıktı).
                # Kılıfın nasıl kurulacağını dala özel few-shot havuzu zaten gösteriyor.
                "gibi gösteren, gereksiz derecede kurumsal bir kılıf kur. Abartılı ünlem/vurgu ya da "
                "savunmacı ısrar KULLANMA - sakin ol, fazla açıklama yapma. Birinci tekil şahıs ya da kısa "
                "kurumsal öbek; 'edilmiştir' gibi pasif kalıp YAZMA. Bu bir YETERLİ not DEĞİL: sıradan gerekçe "
                "değil, şişirilmiş kurumsal kılıf. Kusur/prosedürden bahsetme. Sadece açıklama metnini yaz."
            )
        # Bağlam YALNIZ gizleme/zorunluluk dallarına (baglam_notu_uret filtreler):
        # o iki dal anomalili faturalarda çalışır, etiket metinden bağımsızdır.
        _baglam, _bmeta = baglam_notu_uret(persona, baskin_ham, manipulatif_dal=dal)
        talimat += _baglam
        meta.update(_bmeta)
        # kurnaz/bariz TEMİZ faturalarda çalışır -> gerçekçi bağlam yerine şişirilmiş
        # KILIF alır: yönlendirme olur ama metin `yeterli`ye kaymaz.
        if dal in ("kurnaz", "bariz"):
            _kilif, _kmeta = kilif_notu_uret(baskin_ham)
            talimat += _kilif
            meta.update(_kmeta)
        talimat += f" Firma adını kullanacaksan '{firma_ekli}' biçiminde kullan."
    else:  # ai_uretimi
        kapanis = random.choice(AI_URETIMI_KAPANIS_IPUCLARI)
        acilis = random.choice(AI_URETIMI_ACILIS_IPUCLARI)
        meta["kapanis"] = kapanis
        # Gerçek bir AI, fiş görselinden firma adını da fatura no/tarihi de OKUR ve
        # zaman zaman gereksiz belge ayrıntısı vererek kendini ele verir. Bu yüzden
        # "firma adını ASLA kullanma" yasağı kaldırıldı; ayrıntılar UYDURULMASIN diye
        # gerçek değerler prompt'a veriliyor (fişle çelişen sahte tarih/no üretmesin).
        # TEK meta detay: ikisi birden istendiğinde model uzun bir liste cümlesi kurup
        # token tavanına takılıyor ve cümle ORTASINDAN kesiliyordu (pilot #29, 412 krk).
        meta_detay = random.random() < 0.30
        if meta_detay:
            if random.random() < 0.5:
                detay = f"fiş tarihi {fatura.get('fatura_tarihi', '')}"
            else:
                detay = f"fatura no {fatura.get('fatura_no', '')}"
            meta_notu = (f" Ayrıca bir AI'ın yapacağı gibi TEK bir gereksiz belge ayrıntısına gönderme "
                         f"yap: {detay} (verilen değeri AYNEN kullan, uydurma; başka belge ayrıntısı ekleme).")
        else:
            meta_notu = ""
        talimat = (
            "KARAKTER: AI_URETIMI (tek istisna). İnsan değil, ChatGPT gibi bir yapay zeka gibi yaz: duygusuz, "
            "aşırı resmi, kalıpsal/şablon. Diğer üçünün aksine doğallık ARANMIYOR; bariz yapay/robotik dursun. "
            # PEMBE FİL DÜZELTMESİ: burada eskiden "hep aynı kalıbı ('Belirtilen fiş...')
            # tekrarlama" yazıyordu. Yasaklanan ifadeyi ADIYLA anmak tam tersini yaptı:
            # qwen3 pilotunda 8 ai_uretimi çıktısının 4'ü tam o ifadeyle BAŞLADI, 2'si daha
            # içinde geçirdi (havuzda 20 açılış var, beklenen ~0,4). Kural artık yalnız
            # HEDEFLENEN davranışı söylüyor (docs/arsiv/faz-b-prompt.md §6 pozitif çerçeveleme).
            f"Cümleye '{acilis}' gibi bir açılışla başla ve '...{kapanis}' ifadesiyle bitir; verilen açılış/"
            f"kapanışı AYNEN KULLAN, kendi kalıbını uydurma. Kısa tek cümle de olur, resmi "
            # Çekimli firma adı ai_uretimi'ye de verilir: firma_ek_hatasi kuralı üç
            # kategoride birden çalışıyor, ipucu yalnız yeterli/manipulatif'e verilince
            # ai_uretimi ipuçsuz denetleniyordu ('Nihle'dan' -> doğrusu '-den').
            f"1-2 cümle de -- 3+ cümlelik paragraf yazma. Satıcı/firma adını kullanabilirsin "
            f"(kaynak olarak: {firma_ekli}).{meta_notu}"
        )

    # Few-shot SADECE user prompt'a girer (system prompt sabit -> Ollama önbelleği
    # korunur). yetersiz'de few-shot KULLANILMAZ: talimattaki dönüşümlü örneklerle
    # çakışıp verbatim kopyayı besliyordu ('genel ofis ihtiyacı' collapse'ı).
    # manipulatif'te few-shot DALA göre seçilir: ortak havuz kullanılırken kurnaz dal
    # "abartılı vurgu KULLANMA" derken prompt'una vurgulu örnekler giriyordu.
    fs_blok = "" if kategori == "yetersiz" else fewshot_blok(kategori, adet=2, dal=meta.get("dal"))

    # Persona da user prompt'ta; ai_uretimi hariç (robotik/kişiliksiz kalmalı).
    persona_notu = "" if kategori == "ai_uretimi" else f"Yazan kişi: {persona_metni(persona)}.\n"

    # USER PROMPTU (Sadece değişen kısım) + persona + kategoriden bağımsız uzunluk hedefi.
    user_prompt = (
        f"Satıcı/Firma: {firma_kisa}\nFiş kalemleri: {kalem_ozeti}\n{fs_blok}{persona_notu}"
        # Uzunluk hedefine SAYISAL tavan eşlik eder: niteliksel tarif ('1-2 cümle')
        # tek başına zayıf bir sınır, model rahatça iki katına çıkıyor (T1'de ölçüldü).
        f"Talimat: {talimat}\nUzunluk hedefi: {uz_tarif} — en fazla ~{uz_ust} karakter."
    )

    return system_prompt, user_prompt, meta


# Session oluştur (Global seviyede) -- keep-alive bağlantı havuzu için
http_session = requests.Session()

# ---------------------------------------------------------------------------
# Çıktı temizleme: model bazen <think>, düşünce önsözü ('Tamam, ...'), ön-ek
# ('Açıklama:') ya da tırnak/madde işareti sarması ekliyor. Bunları ayıklamak
# bozuk çıktı kaynaklı gereksiz retry'ı azaltır (hem kalite hem hız kazancı).
# ---------------------------------------------------------------------------

_THINK_REGEX = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_DUSUNCE_ONSOZ = re.compile(
    r"^\s*(okay|alright|let me|let's|sure|first|tamam|bakalım|öncelikle|"
    r"hmm|well|i need to|i should|i'll|the user|kullanıcı)\b.*",
    re.IGNORECASE,
)
# DİKKAT -- `\b` ve ZORUNLU İKİ NOKTA ŞART. Eski desen (`[\s:]*`, sınır yok) ön-eki
# normal Türkçe kelimelerin başında da yakalayıp metni kesiyordu:
#   'İşten dolayı acil ihtiyaç vardı.' -> 'n dolayı acil ihtiyaç vardı.'
#   'Notebook aldım ekip için.'        -> 'ebook aldım ekip için.'
# Bu bozulma qwen3 koşularında da sessizce oluyordu (T1 pilotunda fark edildi).
# Artık ön-ek ancak GERÇEKTEN ön-ekse ('Açıklama:', 'İşte açıklama:') siliniyor.
_ON_EK_TEMIZLE = re.compile(
    r"^(Açıklama|Not|Masraf Açıklaması|İşte|Cevap)\b[^:\n]{0,40}:\s*", re.IGNORECASE
)

# Model rol yapmayı bırakıp NE YAPTIĞINI anlatan bir not eklerse (reasoning
# modellerinde sık): '*(Not: Yetersiz çalışan olarak yazıldığı için...)*',
# '*(Tek cümle, 6-10 kelime: "...")*'. Bunlar ayrı satır olarak gelir ve
# GROUND-TRUTH kategori adını metne taşıdığı için veri setine sızmaları
# leakage'tır. Satır düzeyinde ayıklanır; inline gelirse `meta_sizinti` kuralı
# (ihlalleri_bul) yakalayıp retry tetikler.
# İki yol: (a) parantez/köşeli parantezle açılıp meta kelimeyle devam eden satır,
# (b) meta kelime + iki nokta ile başlayan satır. Sadece kelimeyi aramak YANLIŞ
# POZİTİF verirdi -- 'Not defteri aldım' geçerli bir açıklamadır.
_META_ANAHTARLARI = (
    r"not|uyarı|uyari|tek cümle|tek cumle|karakter|talimat|kategori|açıklama notu|aciklama notu"
)
_META_YORUM_SATIRI = re.compile(
    rf"^[\*\s]*(?:[\(\[][\*\s]*(?:{_META_ANAHTARLARI})\b|(?:{_META_ANAHTARLARI})\s*:)[^\n]*$",
    re.IGNORECASE,
)
# Meta yorum ayrı satır yerine metnin SONUNA eklenmişse (aynı satırda) kuyruğu kes.
# Satır-İÇİ markdown vurgusu. Kenar temizliği (`.strip('*')`) yalnız uçları alıyordu;
# T1 çıktılarının 4/31'inde metnin ORTASINDA '**Tarım Kredi Kooperatifi**' gibi
# kalıntı vardı (qwen3'te 0). Çalışanın masraf notunda markdown olmaz -> ayıkla.
_MARKDOWN_VURGU = re.compile(r"\*\*|__|`")

_META_KUYRUK = re.compile(
    rf"[\*\s]*[\(\[][\*\s]*(?:{_META_ANAHTARLARI})\b[^\)\]]*[\)\]][\*\s]*$", re.IGNORECASE
)


def cikti_temizle(metin: str) -> str:
    """Ham yanıttan düşünce/ön-ek/sarma temizler, kalan anlamlı satırları TEK metne
    birleştirir.

    Eskiden yalnız İLK anlamlı satır dönüyordu; "2-3 cümle" uzunluk hedefi alan
    üretimlerde model tek '\\n' ile satır kırınca metnin gerisi sessizce atılıyor,
    ardından `uzunluk` ihlali doğup boşuna retry tetikliyordu. Çift satır sonu zaten
    stop dizisiyle (`stop=["\\n\\n"]`) kesildiği için kalan satırlar aynı açıklamanın
    parçasıdır. VS akışı bu fonksiyondan geçmez (ham=True)."""
    metin = _THINK_REGEX.sub("", metin)
    metin = _MARKDOWN_VURGU.sub("", metin).strip()
    satirlar = [s.strip() for s in metin.splitlines() if s.strip()]
    temiz = [s for s in satirlar if not _DUSUNCE_ONSOZ.match(s)]
    # Meta yorum satırlarını (model kendi çıktısını açıklıyor) at -- ama HEPSİ
    # meta ise elde bir şey bırak: o zaman `meta_sizinti` kuralı ihlali görsün ve
    # retry tetiklensin (sessizce boş metin döndürmek teşhisi zorlaştırırdı).
    meta_suzulmus = [s for s in temiz if not _META_YORUM_SATIRI.match(s)]
    if meta_suzulmus:
        temiz = meta_suzulmus
    aday_liste = temiz or satirlar
    parcalar: list[str] = []
    for s in aday_liste:
        # Ön-ek ('Açıklama:', 'İşte...') yalnız metnin BAŞINDA anlamlı.
        s2 = _ON_EK_TEMIZLE.sub("", s) if not parcalar else s
        s2 = s2.strip().strip('"\'“”‘’*`>-').strip()
        if len(s2) >= 3 and re.search(r"[a-zçğıöşüA-ZÇĞİÖŞÜ]{2,}", s2):
            parcalar.append(s2)
    if parcalar:
        # Aynı satıra iliştirilmiş meta kuyruğunu da kes ('... aldım *(Not: ...)*').
        return _META_KUYRUK.sub("", " ".join(parcalar)).strip()
    if aday_liste:
        return _ON_EK_TEMIZLE.sub("", aday_liste[-1]).strip().strip('"\'“”‘’*`>-').strip()
    return ""


_CUMLE_SONU = re.compile(r"(?<=[.!?…])\s+")


def uzunluk_buda(metin: str, uz_ust: int, tolerans: float = 1.15) -> str:
    """Hedef tavanı aşan metni TAM CÜMLE sınırından budar.

    Neden gerekli: uzunluk ihlali retry ile güvenilir biçimde düzelmiyor (T1'de
    7/31, retry sonrası bile). num_predict'i kısmak ise cümleyi ORTASINDAN keser --
    bozuk gramer, veri setini zehirler. Budama deterministiktir ve yalnız cümle
    sonlarından keser.

    İlk cümle bile tavana sığmıyorsa metne DOKUNULMAZ: yarım cümle bırakmaktansa
    uzun bırakıp `ihlalleri_bul`'un uzunluk ihlalini görmesi (ve retry/raporun
    tetiklenmesi) yeğdir. Budama sonrası metin yine ihlal denetiminden geçer;
    budama bir dayanağı (kalem/amaç) düşürdüyse ilgili kural zaten yakalar."""
    if not metin or uz_ust <= 0:
        return metin
    tavan = int(uz_ust * tolerans)
    if len(metin) <= tavan:
        return metin
    parcalar = _CUMLE_SONU.split(metin.strip())
    tutulan: list[str] = []
    for p in parcalar:
        aday = " ".join(tutulan + [p])
        if tutulan and len(aday) > tavan:
            break
        tutulan.append(p)
        if len(aday) > tavan:   # ilk cümle zaten taşıyor -> dokunma
            return metin
    budanmis = " ".join(tutulan).strip()
    return budanmis if budanmis else metin


def _red_mi(metin: str) -> bool:
    """Model üretmek yerine reddetmiş mi (moderasyon/güvenlik refleksi)?"""
    d = metin.lower()
    return any(k in d for k in RED_KALIPLARI)


def ollama_cagir(
    system_prompt: str,
    user_prompt: str,
    model: str,
    host: str,
    num_predict: int = 90,
    keep_alive: str | int | None = None,
    temperature: float = 0.9,
    seed: int | None = None,
    min_p: float = 0.1,
    stop: list[str] | None = None,
    ham: bool = False,
    num_ctx: int = 2048,
    bilgi: dict | None = None,
) -> str:
    # OpenAI-uyumlu sağlayıcı seçiliyse o yola dallan (bkz. SAĞLAYICI KATMANI).
    # Aşağıdaki Ollama gövdesi olduğu gibi korunur.
    # 'vllm' de BURAYA girmek zorunda: kendi vLLM sunucun da OpenAI-uyumlu.
    # Koşul yalnız "groq" iken vllm sessizce Ollama gövdesine düşüyor ve
    # /api/generate'e gidip 404 alıyordu (Kaggle koşusunda 500 fatura kaybı).
    if _SAGLAYICI in ("groq", "vllm"):
        return _groq_cagir(
            system_prompt, user_prompt, model, host, num_predict,
            temperature, seed, stop, ham, bilgi, min_p,
        )

    profil = model_profili(model)
    istek_govdesi: dict = {
        "model": model,
        "stream": False,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
            "num_ctx": num_ctx,
            # Yerel model için önerilen: temperature + min_p (top_p'den daha sağlam;
            # yüksek sıcaklıkta aday kümesini şişirmez -> çeşitlilik ↑ tutarlılık korunur,
            # bkz. arXiv:2407.01082). min_p kategoriye göre geçilir (yeterli 0.05, diğerleri 0.1).
            # repeat_penalty tekrarı kırar. seed None ise Ollama rastgele seçer; retry'da
            # farklı seed vererek gerçekten farklı üretim.
            "top_p": 0.95,
            "min_p": min_p,
            "repeat_penalty": 1.15,
        },
    }

    # Model profiline göre istek biçimi (bkz. MODEL_PROFILLERI).
    if profil["raw_chatml"]:
        istek_govdesi["raw"] = True   # Ollama şablonunu uygulama, prompt'u aynen gönder
        istek_govdesi["prompt"] = chatml_sar(system_prompt, user_prompt, profil["think_prefill"])
    else:
        istek_govdesi["system"] = system_prompt
        istek_govdesi["prompt"] = user_prompt
    # think alanı: None ise gövdeye HİÇ konmaz (şablonu desteklemeyen modelde
    # alanın varlığı ya hata verir ya da sessizce yanıltır).
    if profil["think_alani"] is not None:
        istek_govdesi["think"] = profil["think_alani"]

    if seed is not None:
        istek_govdesi["options"]["seed"] = seed
    # stop: çok-paragraflı gevezeliği kes (num_ctx/num_predict israfını önler).
    # Profil stop tanımlıyorsa çağıranınkini EZER -- raw modda `\n\n` prefill'i
    # anında kesip boş metin üretirdi.
    stop = profil["stop"] or stop
    if stop:
        istek_govdesi["options"]["stop"] = stop
    # keep_alive üst-seviye alandır (options içinde değil); burst boyunca modelin
    # bellekte kalması için runner bunu geçebilir.
    if keep_alive is not None:
        istek_govdesi["keep_alive"] = keep_alive

    yanit = http_session.post(f"{host}/api/generate", json=istek_govdesi, timeout=60)
    yanit.raise_for_status()

    govde = yanit.json()
    yanit_metni = govde.get("response", "")
    # done_reason == "length" -> token bütçesi bitti, cümle ORTASINDAN kesildi
    # ("...katılım giderleri kapsamında gerçekleştirilme"). Bu bilgiyi okumuyorduk;
    # kesik metin hiçbir kurala takılmadan veri setine giriyordu. Çağıran `bilgi`
    # dict'i geçerse `kesik` ihlali üretip retry tetikleyebilir.
    if bilgi is not None:
        bilgi["done_reason"] = govde.get("done_reason")
    if ham:
        # VS için: çok satırlı ham yanıt (yalnız <think> ayıklanır), aday ayrıştırma
        # tek satıra indirgenmeden yapılabilsin.
        return _THINK_REGEX.sub("", yanit_metni).strip()
    return cikti_temizle(yanit_metni)


def modeli_bellekten_indir(model: str, host: str) -> None:
    """
    keep_alive=0 ile boş bir çağrı yaparak modeli Ollama belleğinden indirir.
    Cooldown başında çağrılır -- RAM serbest kalır, CPU/GPU soğur. Hata olursa
    sessizce geçer (cooldown akışını bozmasın).
    """
    try:
        http_session.post(
            f"{host}/api/generate",
            json={"model": model, "prompt": "", "stream": False, "keep_alive": 0},
            timeout=30,
        )
    except Exception:
        pass


YASAKLI_PASIF_KALIPLAR = ("edilmiştir", "edildi", "sağlanmıştır", "karşılanmıştır", "alınmıştır")

# ---------------------------------------------------------------------------
# EDİLGEN / 3. ŞAHIS DENETİMİ (pasif_kalip'in morfolojik hali)
# ---------------------------------------------------------------------------
# Sabit 5 kalıplık liste yetersizdi: pilot koşusunda 24 insan çıktısının 8'i (%33)
# edilgen ya da 3. şahıs geldi ve HİÇBİRİ yakalanmadı -- 'aldılar', 'alınmış',
# 'karşılandı', 'alındı', 'kullanıldı', 'gerçekleştirildi', 'aldı'. Oysa hem sistem
# promptu hem karakter tanımı birinci tekil şahıs istiyor (çalışan kendi notunu yazar).
#
# KELİME SINIRI ŞART: substring denetimi yapılırsa 'aldı' -> 'aldım'ı da yakalar ve
# doğru çıktıyı ihlal sayar. \b ile 'aldım'/'aldık' (1. şahıs) SERBEST kalır,
# 'aldı'/'aldılar' (3. şahıs) yakalanır.
_PASIF_KOKLER = (
    "alın", "kalın", "karşılan", "kullanıl", "yapıl", "öden", "veril", "edil",
    "gerçekleştiril", "sağlan", "temin edil", "tedarik edil", "tamamlan", "gönderil",
)
_PASIF_REGEX = re.compile(
    r"\b(?:" + "|".join(_PASIF_KOKLER) + r")(?:dı|di|du|dü|mış|miş|muş|müş)(?:tır|tir|tur|tür)?\b",
    re.IGNORECASE,
)

# 3. şahıs (tekil/çoğul) geçmiş zaman -- YALNIZCA bilinen fiil kökleriyle; serbest
# ek taramasi 'toplantılar' gibi isimleri yanlişlikla yakalıyordu.
_UCUNCU_SAHIS_KOKLER = (
    "al", "öde", "kullan", "karşıla", "yap", "ver", "getir", "gerçekleştir",
    "temin et", "tedarik et", "satın al",
)
_UCUNCU_SAHIS_REGEX = re.compile(
    r"\b(?:" + "|".join(_UCUNCU_SAHIS_KOKLER) + r")(?:dı|di|du|dü|tı|ti|tu|tü)(?:lar|ler)?\b",
    re.IGNORECASE,
)


def _pasif_ya_da_ucuncu_sahis_mi(metin: str) -> bool:
    """İnsan kategorilerinde edilgen ya da 3. şahıs anlatım var mı?
    (ai_uretimi MUAF -- resmi/edilgen dil onun karakteri.)"""
    d = metin.lower()
    return (any(k in d for k in YASAKLI_PASIF_KALIPLAR)
            or bool(_PASIF_REGEX.search(metin))
            or bool(_UCUNCU_SAHIS_REGEX.search(metin)))


# ---------------------------------------------------------------------------
# KATEGORİ ADI SIZINTISI (boşluklu form)
# ---------------------------------------------------------------------------
# `kalemler_ozetle_prompt` kategoriyi insan-okur gösteriyor ('kisisel bakim') ki model
# ham enum'u ('kisisel_bakim') kopyalayamasin. Ama pilotta model bu kez BOŞLUKLU formu
# kopyaladi: 24 insan çiktisinin 7'si (%29) kategori adini ürün adi yerine kullandi
# ('yemek hizmeti aldım', 'teknoloji ekipmanı aldı'). Gerçek bir çalişan kategori adi
# değil ÜRÜN adi yazar ('öğle yemeği', 'tv askısı').
#
# YALNIZCA ÇOK KELİMELİ kategoriler denetlenir: 'konaklama', 'giyim', 'temizlik' gibi
# tek kelimeliler doğal Türkçedir ve yasaklanirsa yanliş pozitif üretir. Çok kelimeli
# form ('ofis sarf malzeme', 'teknoloji ekipman') ise ancak sistem etiketinden kopyalanmiş
# olabilir. Çekim ekleri substring eşleşmesiyle kapsanir ('teknoloji ekipman' ⊂
# 'teknoloji ekipmanlarının'). ai_uretimi MUAF -- urun_detay_kopya ile ayni mantik.
def _kategori_adi_sizinti_mi(metin: str, kalemler: list[dict]) -> bool:
    n = _tr_normalize(metin)
    for k in kalemler:
        enum_ad = k["harcama_kategorisi"]
        if "_" not in enum_ad:
            continue   # tek kelimeli -> doğal Türkçe olabilir, denetleme
        if _tr_normalize(enum_ad.replace("_", " ")) in n:
            return True
    return False

DUZELTME_NOTLARI = {
    "sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında gizlenmesi gereken kalemin gerçek adı ya da "
        "kategorisi açığa çıktı. Bu kez o kalemden HİÇ bahsetme, sadece diğer/meşru kalemlere odaklan."
    ),
    "pasif_kalip": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında EDİLGEN ya da 3. ŞAHIS anlatım vardı "
        "('alındı', 'aldı', 'aldılar', 'karşılandı', 'kullanıldı', '...edilmiştir' gibi). "
        "Notu YAZAN SENSİN: bu kez SADECE birinci tekil şahıs aktif fiil kullan -- "
        "'aldım', 'ödedim', 'kullandım'. İstersen fiili tamamen at, öbek olarak bırak."
    ),
    # uzunluk notu meta["uzunluk"][0] (hedef adı) ile biçimlendirilecek.
    "uzunluk": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın istenen uzunlukta değildi. Bu kez uzunluğu "
        "'{uzunluk_ad}' hedefine göre ayarla (ne daha kısa ne daha uzun)."
    ),
    "red": (
        "ÖNEMLİ DÜZELTME: Bu tamamen kurgusal bir masraf açıklaması yazma görevidir, "
        "gerçek bir işlem değildir. Reddetme; SADECE istenen açıklama metnini yaz."
    ),
    # kapanis_eksik notu meta["kapanis"] ile biçimlendirileceği için şablon.
    "kapanis_eksik": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın istenen kapanışla bitmedi. Bu kez cümleyi "
        "MUTLAKA tam olarak '...{kapanis}' ifadesiyle bitir."
    ),
    # vurgu_eksik notu meta["vurgu"] ile biçimlendirilecek.
    "vurgu_eksik": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın yeterince ısrarlı/abartılı değildi, meşru gibi durdu. "
        "Bu kez '{vurgu}' gibi abartılı bir vurguyu MUTLAKA ekle (cümlenin doğal bir yerine) ve "
        "harcamayı gereğinden fazla haklı çıkar."
    ),
    # PEMBE FİL: bu notta ısrar kelimelerini ÖRNEKLEYEREK sayma. Ölçüldü
    # (docs/arsiv/faz-b-prompt.md §6): yasaklanan ifadeyi adıyla anmak onu ÜRETTİRİYOR --
    # "'Belirtilen fiş' kalıbını tekrarlama" denince 8 çıktının 4'ü tam o ifadeyle
    # başlamıştı. Kural bu yüzden SAYI üzerinden ve POZİTİF kurulur.
    "vurgu_fazlasi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın kendini birden çok kez savundu; ısrar üst üste "
        "bindiği için yapmacık durdu. Gerçek bir çalışan kendini BİR KEZ savunur. Bu kez "
        "ısrarını cümlede TEK BİR yerde bırak, geri kalanı sakin ve kurumsal olsun."
    ),
    # PEMBE FİL: fişte olmayan temayı ADIYLA anma ("çerezden bahsetme" deme),
    # yoksa model tam onu yazar (docs/arsiv/faz-b-prompt.md §6). Yön POZİTİF verilir.
    "tema_halusinasyonu": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın fişte olmayan bir harcamadan söz etti. "
        "Kısa ve muğlak kalman SORUN DEĞİL -- istenen bu. Ama bahsettiğin şey fişteki "
        "alışverişle uyuşmalı: ya fişte gerçekten olan şeyi sade bir dille an, ya da "
        "hiç ürün/tür anma ve tamamen genel kal."
    ),
    "karakter_kirilmasi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın kılıfı bozdu; harcamanın aykırı/şüpheli olduğunu "
        "ya da prosedür/kural gerektiğini ima etti. Bu kez tam tersi: kendinden emin, harcamayı "
        "SORGUSUZ meşru göster; kusur/aykırılık/prosedürden HİÇ bahsetme."
    ),
    "yeterli_halusinasyon": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın fişte OLMAYAN bir tema (ör. 'yemek/ağırlama') uydurdu. "
        "Bu kez YALNIZCA fişteki gerçek kalemlere dayan; olmayan bir harcama türü uydurma."
    ),
    "yeterli_dayanaksiz": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın harcamanın SEBEBİNİ söylemedi (sadece ne aldığını "
        "yazmak yetmez). Bu kez hangi iş bağlamında olduğunu somut belirt: toplantı, müşteri "
        "ziyareti, saha işi, mesai, randevuya yetişme gibi. Kalem ve firma adı isteğe bağlı."
    ),
    # PEMBE FİL (2026-07-30): bu not eskiden ÜÇ kategori adını örnekliyordu
    # ('kisisel_bakim', 'yemek hizmeti', 'teknoloji ekipman') -- hem de retry'da,
    # yani modelin en çok tutunduğu yerde. docs/arsiv/durum-2026-07-29.md
    # "enum_sizinti'nin bir kısmı bizim eserimiz" derken kaynağı prompt talimatı
    # sanıyordu; asıl kaynak büyük olasılıkla buydu. Yasak dizeler kaldırıldı,
    # POZİTİF yön (ürünün kendi adı) korundu -- o kısım zaten doğruydu.
    "enum_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında sistemin sınıflandırma etiketi geçti. "
        "Gerçek bir çalışan öyle yazmaz, ürünün günlük dildeki adını yazar "
        "('öğle yemeği', 'tv askısı', 'şampuan'). Bu kez ürünü kendi adıyla an."
    ),
    # T1 pilotunda (2026-07-28) bu not YETMEDİ: model açıklamanın sonuna kendi
    # yaptığını anlatan bir not ekledi ve GROUND-TRUTH kategori adını metne taşıdı
    # ('**Yeterli çalışan** olarak: Harcamayı net amaçlandı...'). Etiket doğrudan
    # feature'a sızdığı için not artık YASAĞI ADIYLA söylüyor.
    "meta_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın görevi/fişi betimledi ya da nasıl yazdığını "
        "açıkladı. Sadece masraf açıklamasının KENDİSİNİ yaz: sonuna not/parantez içi "
        "yorum EKLEME, 'yeterli/yetersiz/manipülatif/AI' gibi karakter adlarını ve "
        "talimattan aldığın kelimeleri (uzunluk, karakter, kategori) HİÇ kullanma."
    ),
    "latin_disi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında Türkçe/Latin alfabesi dışında karakter vardı. "
        "Bu kez metnin TAMAMINI Türkçe yaz; başka alfabeden tek karakter bile kullanma."
    ),
    "verbatim_kopya": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın verilen örneklerden birinin neredeyse aynısıydı. "
        "Bu kez örnekleri kopyalama; kendi özgün ifadenle, farklı kelimelerle yaz."
    ),
    "urun_detay_kopya": (
        "ÖNEMLİ DÜZELTME: Ürünü ham haliyle (boyut/miktar/paket/model detayıyla) yazdın. "
        "Gerçek bir çalışan böyle yazmaz; SADELEŞTİR: ne olduğunu yaz (ör. 'el sabunu', "
        "'kupa seti', 'sabun'), '500Ml', '4'lü paket', renk/model gibi detayları ATLA."
    ),
    "tutar_sizinti": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında tutar/rakam geçti. Gerçek bir çalışan "
        "açıklamaya fiyat yazmaz; bu kez hiçbir sayı, tutar ya da 'TL' ifadesi kullanma."
    ),
    "firma_icin_hatasi": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabında firma adının ardından '... için' geldi, bu satın "
        "alınan yeri değil amacı gösterir. Firmayı kaynak olarak yaz: 'ABC Yazılım'dan lisans aldık', "
        "'XYZ Market'ten malzeme aldım' gibi doğru '-dan/-den/-tan/-ten' ekiyle."
    ),
    # Doğru biçim prompt'ta ZATEN veriliyor (ayrilma_eki); not onu hatırlatır.
    "kesik": (
        "ÖNEMLİ DÜZELTME: Az önceki cevabın cümlenin ortasında kesildi. Bu kez DAHA KISA "
        "yaz ve cümleyi mutlaka tamamla; yarım bırakma."
    ),
    "firma_ek_hatasi": (
        "ÖNEMLİ DÜZELTME: Firma adına yanlış ek getirdin. Prompt'ta verilen çekimli biçimi "
        "AYNEN kullan, kendin ek uydurma."
    ),
}


_TR_HARF_MAP = str.maketrans({
    "ğ": "g", "ü": "u", "ş": "s", "ı": "i", "ö": "o", "ç": "c",
    "â": "a", "î": "i", "û": "u", "İ": "i",
})


def _tr_normalize(s: str) -> str:
    """Türkçe karakterleri ASCII'ye indirger + küçük harfe çevirir. Kategori
    adları ASCII saklanırken (eglence) modelin doğru Türkçe (eğlence) yazması
    arasındaki eşleşme farkını kapatır.

    DİKKAT -- `\\u0307` (combining dot above) SİLİNMELİ: Python'da 'İ'.lower()
    ASCII 'i' değil 'i̇' (i + birleşen nokta) üretir. Bu yüzden modelin BÜYÜK
    harfle yazdığı her kelime normalize sonrası eşleşmiyordu ve substring'e dayanan
    TÜM kurallar (meta_sizinti, sizinti, enum_sizinti, karakter_kirilmasi) o metinlerde
    sessizce devre dışı kalıyordu. T1 pilotunda yakalandı: metin 'YETERSİZ çalışanın'
    diyordu, `_META_SIZINTI_KALIPLARI`'ndaki 'yetersiz calisan' eşleşmiyordu."""
    return s.lower().replace("̇", "").translate(_TR_HARF_MAP)


def _kok(token: str) -> str:
    """Kaba kök: 4 harften uzun bir kelimenin sondaki tek sesli ekini atar
    ('hizmeti' -> 'hizmet'), böylece çekim ekli biçimler ('hizmetleri') de
    substring olarak yakalanır."""
    return token[:-1] if len(token) > 4 and token[-1] in "iıuüea" else token


# ---------------------------------------------------------------------------
# DEDUP + ÇEŞİTLİLİK: normalize edip token-jaccard ile yakın-kopya tespit,
# distinct-1/2 çeşitlilik metriği. Harici bağımlılık yok; mevcut _tr_normalize'ı
# yeniden kullanır. Tüketiciler (runner/pilot) mode collapse'ı ölçmek/işaretlemek
# için kullanır -- üretim akışına maliyet bindirmez.
# ---------------------------------------------------------------------------

def _dedup_normalize(s: str) -> str:
    s = _tr_normalize(s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_set(s: str) -> set[str]:
    return set(_dedup_normalize(s).split())


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    birlesim = len(a | b)
    return len(a & b) / birlesim if birlesim else 0.0


def yakin_kopya_mi(yeni: str, kabul_edilenler: list[set[str]], esik: float = 0.8) -> bool:
    yt = _token_set(yeni)
    return any(jaccard(yt, var_olan) >= esik for var_olan in kabul_edilenler)


def distinct_n(metinler: list[str], n: int) -> float:
    """distinct-n: benzersiz n-gram / toplam n-gram. 1'e yakın = çeşitli,
    0'a yakın = tekrarlı/mode collapse."""
    toplam, benzersiz = 0, set()
    for m in metinler:
        tok = _dedup_normalize(m).split()
        grams = [tuple(tok[i:i + n]) for i in range(len(tok) - n + 1)]
        toplam += len(grams)
        benzersiz.update(grams)
    return len(benzersiz) / toplam if toplam else 0.0


def _sizinti_var_mi(metin: str, gizlenecek: dict) -> bool:
    """Gizlenmesi gereken kalemin kategorisi ya da ürün adı metne sızmış mı?
    Türkçe morfoloji/diakritik farklarına dayanıklı kök-token eşleşmesi kullanır."""
    n = _tr_normalize(metin)
    # 1) Kategori: tüm kök token'ları metinde geçiyorsa sızıntı (ör. yemek_hizmeti
    #    -> 'yemek' VE 'hizmet' ikisi de varsa). Tümünü şart koşmak yanlış pozitifi azaltır.
    kategori_kokler = [_kok(t) for t in _tr_normalize(gizlenecek["harcama_kategorisi"]).split("_") if t]
    if kategori_kokler and all(kok in n for kok in kategori_kokler):
        return True
    # 2) Ürün adı: anlamlı (>3 harf) kelimelerinden herhangi biri geçiyorsa sızıntı.
    for kelime in _tr_normalize(gizlenecek["aciklama"]).split():
        if len(kelime) > 3 and kelime in n:
            return True
    return False


# Resmî/edilgen AI kapanışının MORFOLOJİK imzası: '-mIştIr' (gerçekleştirilmiştir,
# sağlanmıştır, temin edilmiştir...). Havuz ÜYELİĞİ tek başına şart koşulursa
# ai_uretimi veri sette kapalı bir string kümesine indirgenir (sınıflandırıcı üslubu
# değil listeyi ezberler) ve havuz her büyüdüğünde validator'ı güncellemek gerekir.
_AI_RESMI_KAPANIS = re.compile(r"(mış|miş|muş|müş)t[ıi]r\b", re.IGNORECASE)


def _kapanis_var_mi_herhangi(metin: str) -> bool:
    """Metin resmî/edilgen bir AI kapanışı taşıyor mu? İki yoldan biri yeter:
    (a) '-mIştIr' morfolojik imzası, (b) havuzdaki kapanışlardan birinin geçmesi.
    'Bitiyor mu' değil 'içeriyor mu' esnekliği korunur (kapanıştan sonra minik bir
    ek gelmesi boşuna retry üretmesin)."""
    if _AI_RESMI_KAPANIS.search(metin):
        return True
    normalize = metin.lower()
    return any(k.lower() in normalize for k in AI_URETIMI_KAPANIS_IPUCLARI)


def _vurgu_var_mi(metin: str, vurgu: str | None = None) -> bool:
    """manipulatif 'aşırı haklı çıkarma' metninde abartılı vurgu işareti var mı?
    Önce ATANAN vurgunun ayırt edici kelimeleri aranır (havuz genişledikçe kural
    otomatik uyum sağlar), sonra genel emphatic-marker fallback denetlenir."""
    n = _tr_normalize(metin)
    if vurgu:
        vurgu_kok = [_tr_normalize(w) for w in vurgu.split() if len(w) > 3]
        if vurgu_kok and any(k in n for k in vurgu_kok):
            return True
    return any(anahtar in n for anahtar in _VURGU_ANAHTARLARI)


def _vurgu_sayisi(metin: str) -> int:
    """Metinde kaç FARKLI abartılı ısrar işareti geçiyor?

    `vurgu_eksik`in eşi olan `vurgu_fazlasi` için (2026-07-30). Gerekçe kullanıcı
    incelemesinden: "eksiksiz biçimde ... yüzde yüz işle alakalıdır ... hiçbir
    eksiklik yok" -- üç ısrar aynı metinde, insan gözüyle yapmacık duruyor.
    Beğenilen manipulatif örneklerin ortak özelliği ısrarın TEK olması.

    Sayım FARKLI anahtar üzerinden: aynı ifadenin tekrarı değil, üst üste binen
    ayrı ısrar kalıpları hedefleniyor. Ölçüldü (200 kayıtlık pilot): manipulatif
    çıktıların %11'i (4/35) 2+ işaret taşıyor -- kural dar, retry'ı patlatmaz."""
    n = _tr_normalize(metin)
    return sum(1 for anahtar in _VURGU_ANAHTARLARI if anahtar in n)


# --- Faz 2: ek kural-tabanlı ihlal denetimleri (judge yerine) --------------

def _enum_sizinti_var_mi(metin: str, kalemler: list[dict]) -> bool:
    """Ham kategori enum adı ('kisisel_bakim' gibi alt çizgili) açıklamaya sızmış mı?
    Model bazen etiketi olduğu gibi yazıyor -> sınıflandırıcıya bedava sinyal (#48)."""
    d = metin.lower()
    return any("_" in k["harcama_kategorisi"] and k["harcama_kategorisi"] in d for k in kalemler)


# Görev/etiketi betimleyen (rol yapmayı bırakan) kalıplar. ai_uretimi'nin meşru
# resmi dilini ('fiş kapsamında', 'fiş içeriğinde') YAKALAMAYACAK kadar dar tutuldu.
_META_SIZINTI_KALIPLARI = (
    "yeterli degil", "yetersiz degil", "gibi gorunuyor", "gibi duruyor",
    "aciklama yaz", "aciklama gir", "bu bir aciklama", "bu bir yeterli",
    "bu bir yetersiz", "bu bir manipulat", "aciklama metni",
    # GROUND-TRUTH kategori adının metne sızması. Reasoning modelleri (T1) rol
    # yapmayı bırakıp "hangi karakteri oynadığını" yazabiliyor:
    #   'gerekliydi aldım işte (Not: YETERSİZ ÇALIŞAN olarak yazıldığı için...)'
    # Bu, etiketin doğrudan feature'a sızması demektir -> ihlal say, retry tetikle.
    # cikti_temizle ayrı satırdaki meta yorumu zaten atıyor; bu kural inline
    # kalanlar için backstop.
    "yeterli calisan", "yetersiz calisan", "manipulatif calisan", "ai uretimi",
    "karakter olarak", "karaktere gir", "talimatlara uy", "talimata uy",
    "istenen karakter", "kelime siniri", "uzunluk hedefi",
)


def _meta_sizinti_var_mi(metin: str) -> bool:
    """Model rol yapmayı bırakıp görevi/etiketi betimlemiş mi? (#12: 'masraf fişi
    içeriği yeterli değilmiş gibi görünüyor')."""
    n = _tr_normalize(metin)
    return any(k in n for k in _META_SIZINTI_KALIPLARI)


# manipulatif KARAKTER KIRILMASI: kılıfı bozup ihlali itiraf/prosedür anlatma
# (#92 'politikalarına aykırıdır', #56 'yazılı olması gerekmektedir', #93 'fazla mı').
_MANIP_KIRILMA_KALIPLARI = (
    "aykiri", "yazili olmasi", "fazla mi", "uygun degil", "supheli",
    "odenmemeli", "sorgulan", "prosedur", "politikalara ayk",
)


def _karakter_kirilmasi_mi(metin: str) -> bool:
    n = _tr_normalize(metin)
    return any(k in n for k in _MANIP_KIRILMA_KALIPLARI)


# yeterli HALÜSİNASYON: fişte gerçekten olmayan bir tema uydurma. Gözlenen baskın
# hata "müşteri yemeği" dolgusu (#75 teknoloji, #94 kişisel bakım fişinde). Tema
# kelimesi geçiyor ama fişte ilgili kategori YOKSA halüsinasyon.
# NOT: "ağırlama/ikram" YEMEK teması SAYILMAZ -- 'müşteri ağırlamak' gıda gerektirmeyen
# meşru bir iş amacıdır (bkz. iyi örnek #66: ağırlama için araç kiralama). Yalnız açık
# yemek kelimeleri tetikler.
_YEMEK_TEMA = ("yemek", "yemegi", "yedik", "kahvalti", "restoran")
_YEMEK_KATEGORILERI = {"yemek_hizmeti", "konaklama", "temel_gida"}

# TEMA -> o temayi MESRU kilan harcama kategorileri (2026-07-30).
# `_yeterli_halusinasyon_mi`nin yalnizca YEMEK ekseninde yaptigi denetimin
# genellestirilmis hali; `yetersiz` icin de kullanilir (bkz. _tema_halusinasyonu_mi).
#
# TASARIM SINIRI (kullanici, 2026-07-30): `yetersiz`in kusuru AMACIN yoklugudur,
# kalemin yoklugu DEGIL. "mobilya alimi", "kirtasiye aldim", "genel gider",
# "aldim iste" hepsi GECERLI yetersiz ornekleridir ve bu kural onlara
# DOKUNMAMALIDIR. Yakalanan tek sey: fiste KARSILIGI OLMAYAN bir temayi anmak
# ('cerezli alisveris' -> fiste deodorant + sabun var).
#
# Kelime secimi DAR: kisa/cok anlamli kokler (masa -> masaj, kalem -> ...)
# bilerek DISARIDA. Yanlis pozitif, kacirilan halusinasyondan pahalidir --
# `yetersiz` zaten mugllak olmak ZORUNDA, onu ihlalle cezalandirmak kategoriyi bozar.
_TEMA_KATEGORI: list[tuple[str, set[str]]] = [
    (r"\b(yemek|yemegi|yedik|kahvalti|ogle yemegi|aksam yemegi|restoran|cerez|"
     r"atistirmalik|icecek|kahve mola)", {"yemek_hizmeti", "temel_gida", "konaklama"}),
    (r"\b(temizlik|deterjan|hijyen)", {"temizlik"}),
    (r"\b(kirtasiye|toner|fotokopi)", {"ofis_sarf_malzeme"}),
    (r"\b(mobilya|koltuk takimi)", {"ofis_mobilya"}),
    (r"\b(konaklama|otelde|gecelik|pansiyon)", {"konaklama"}),
    (r"\b(ulasim|taksi|yakit|akaryakit|ucak bileti|otobus bileti)",
     {"ulasim_hizmeti", "ulasim_bireysel"}),
    (r"\b(yazilim|lisans|abonelik)", {"yazilim_lisans"}),
    (r"\b(bilgisayar|donanim|teknoloji ekipman)", {"teknoloji_ekipman"}),
    (r"\b(danismanlik|musavirlik)", {"danismanlik"}),
    (r"\b(giyim|kiyafet)", {"giyim"}),
    (r"\b(kozmetik|sampuan)", {"kisisel_bakim"}),
]
_TEMA_DESENLERI = [(re.compile(d), kats) for d, kats in _TEMA_KATEGORI]

# Kategori enum'unun GUNLUK DILDEKI karsiligi (2026-07-30). `yetersiz` icin:
# "ofis mobilya alimi" hem enum sizintisidir hem de urunun NEREYE alindigi
# bilgisini tasir -- oysa `yetersiz`in isi bu bilgiyi VERMEMEK. "mobilya aldim"
# ikisini birden cozer.
#
# MEKANIK BIR KURAL DEGIL (ilk kelimeyi atmak) -- her kategori icin tek tek
# secildi, cunku mekanik yol dort yerde bozuluyordu:
#   ulasim_hizmeti + ulasim_bireysel -> ikisi de "ulasim"a duserdi (ayirt edilemez)
#   teknoloji_ekipman -> "teknoloji" ayni zamanda bir IsKolu adi
#   kisisel_bakim     -> "bakim" cok anlamli (arac bakimi)
#   tutun_urunleri    -> yasakli kategori, genel ad ONERILMEZ
# Haritada OLMAYAN kategori icin ipucu hic verilmez (yasakli dortlu bilerek yok).
_KATEGORI_GENEL_AD: dict[str, str] = {
    "yemek_hizmeti": "yemek",
    "temel_gida": "market alışverişi",
    "ulasim_hizmeti": "nakliye",
    "ulasim_bireysel": "ulaşım",
    "konaklama": "konaklama",
    "ofis_sarf_malzeme": "kırtasiye",
    "ofis_mobilya": "mobilya",
    "teknoloji_ekipman": "ekipman",
    "yazilim_lisans": "lisans",
    "danismanlik": "danışmanlık",
    "giyim": "giyim",
    "kisisel_bakim": "bakım ürünü",
    "temizlik": "temizlik malzemesi",
}


def _fatura_kaynak_kelimeleri(fatura: dict) -> list[str]:
    """Faturadan gelen (firma adı + kalem adları) normalize kelimeler."""
    kaynaklar = [firma_adi_kisalt(fatura["satici_unvan"])]
    kaynaklar += [k["aciklama"] for k in fatura["kalemler"]]
    kelimeler = []
    for kaynak in kaynaklar:
        kelimeler += [w for w in _tr_normalize(kaynak).split() if len(w) > 2]
    return kelimeler


def _yeterli_halusinasyon_mi(metin: str, fatura: dict) -> bool:
    n = _tr_normalize(metin)
    # Firma adı ve kalem adları taramadan DÜŞÜLÜR: registry'de adında 'restoran/yemek'
    # geçen 161 OSM firması var ve sistem promptu firma adını kullanmaya izin veriyor.
    # Aksi halde is_kolu uyumsuzluğu anomalisinde ("Keyif Restoran'dan teknoloji ürünü
    # aldım") halüsinasyon flag'i düşüyor ve firma adı değişmediği için retry ile
    # DÜZELTİLEMİYOR -> kalan-ihlalli çıktı.
    for kel in _fatura_kaynak_kelimeleri(fatura):
        n = n.replace(kel, " ")
    if not any(t in n for t in _YEMEK_TEMA):
        return False
    return not any(k["harcama_kategorisi"] in _YEMEK_KATEGORILERI for k in fatura["kalemler"])


def _tema_halusinasyonu_mi(metin: str, fatura: dict) -> bool:
    """Metin, fişte KARŞILIĞI OLMAYAN bir harcama temasını anıyor mu?

    Ölçülen vaka (2026-07-30 pilotu): fişte 'Miss hair'den deodorant + sabun
    (kisisel_bakim) varken model 'çerezli alışveriş' yazdı ve HİÇBİR ihlal almadı --
    `_yeterli_halusinasyon_mi` yalnız kategori=='yeterli'de çağrılıyordu.

    KALEM ADINI ANMAK İHLAL DEĞİLDİR: firma ve kalem kelimeleri taramadan
    düşülür (aynı `_yeterli_halusinasyon_mi` mantığı), yani fişteki ürünü doğru
    anan bir metin buradan geçer. Yakalanan tek şey, fişte dayanağı olmayan
    bir tema. Hiç tema anmayan muğlak metinler ('genel gider', 'aldım işte')
    de geçer -- `yetersiz`in tanımı zaten budur."""
    n = _tr_normalize(metin)
    for kel in _fatura_kaynak_kelimeleri(fatura):
        n = n.replace(kel, " ")
    mevcut = {k["harcama_kategorisi"] for k in fatura["kalemler"]}
    for desen, kategoriler in _TEMA_DESENLERI:
        if desen.search(n) and not (kategoriler & mevcut):
            return True
    return False


def _yeterli_dayanaksiz_mi(metin: str, fatura: dict) -> bool:
    """yeterli açıklama İŞ AMACINI/BAĞLAMINI söylemek ZORUNDA.

    KURAL DEĞİŞTİ (2026-07-28): eskiden "iş amacı VEYA firma adı VEYA gerçek kalem"
    üçlüsünden biri yeterliydi -> salt kalem listesi geçerli sayılıyordu. T1 pilotunda
    ölçüldü: 'Barbekü Soslu Tavuk, Tavuklu Pizza, Hot Dog ve Ispanaklı Gözleme aldım.'
    hiçbir ihlal almadı, oysa `yeterli`nin TANIMI amacı söylemesidir. Yeni sözleşme:

        ZORUNLU : masraf sebebi/bağlamı (toplantı, ziyaret, mesai, randevu, işe geliş...)
        SERBEST : kalem (sadeleştirilmiş), firma adı, neden-sonuç bağlacı
        YASAK   : salt kalem listesi ('X, Y ve Z aldım.')

    Kalem/firma artık tek başına ÇIPA SAYILMAZ; amaç yoksa açıklama pratikte
    `yetersiz`e kaymıştır. Eşleşme token+kök bazlıdır (bkz. _amac_koku_var_mi):
    substring denemesi eskiden 2 harflik "is" girdisiyle 'kişisel'/'sistem'/'bisiklet'
    gibi kelimelerde eşleşip kuralı fiilen devre dışı bırakıyordu."""
    n = _tr_normalize(metin)
    n_tok = set(re.findall(r"\w+", n))
    # FİRMA ADI TOKEN'LARI DÜŞÜLÜR: registry'deki 28.882 firmanın 2.238'i (%7,7) adında
    # bir amaç kelimesi taşıyor (danismanlik 766, organizasyon 419, denetim 381,
    # etkinlik 367, seyahat 199...). Firma adı anıldığı anda kural kendiliğinden tatmin
    # oluyordu: "Üzerimdeki stresle başa çıkmak için ... Kaçira Organizasyon'dan aldım."
    # amaç TAŞIMADIĞI hâlde geçiyordu. Amaç, firma adı DIŞINDA bir yerden gelmeli.
    # (Aynı ilke _yeterli_halusinasyon_mi'de zaten var: firma/kalem adları taranmaz.)
    for kaynak in (fatura["satici_unvan"], firma_adi_kisalt(fatura["satici_unvan"])):
        n_tok -= set(re.findall(r"\w+", _tr_normalize(kaynak)))
    # 1) çok-kelimeli iş-amacı/bağlam ifadesi (substring aranması güvenli)
    if any(ifade in n for ifade in _YETERLI_AMAC_IFADELERI):
        return False
    # 2) iş-amacı kökü (ünsüz yumuşaması dahil: 'ekibine' -> 'ekip')
    return not _amac_koku_var_mi(n_tok)


# İNSAN-vs-AI AYRACI: gerçek bir çalışan ürünü SADELEŞTİRİR ('F Saff Sıvı El Sabunu
# Meyve Aromalı 500Ml' -> 'el sabunu'); boyut/miktar/paket/model gibi detayları elle
# yazmaz. AI ise ham ürün adını olduğu gibi taşır. Bu "ürün-detay gürültüsü" 3 insan
# kategorisinde ihlaldir; ai_uretimi'nde serbesttir (kasıtlı -> ayraç sinyali).
# Servis adlarında (iş sağlığı ve güvenliği hizmeti vb.) YANLIŞ POZİTİF olmasın diye
# yalnız sayı+birim/paket gürültüsüne bakılır, kelime uzunluğuna değil.
_URUN_DETAY_GURULTU = re.compile(
    r"\b\d+[.,]?\d*\s*(?:ml|cl|gr|kg|lt|cm|mm|mah|watt|kwh|kw|gb|tb|mb|inç|inch|dpi|hz|w)\b"
    r"|\b\d+\s*['’´`]?\s*l[iıuü]\b"      # 4'lü, 3 li, 5lu (paket-adet)
    r"|\bpkt\b",
    re.IGNORECASE,
)


# TUTAR/RAKAM SIZINTISI: sistem promptu insan kategorilerinde rakam yasaklıyordu ama
# denetlenmiyordu. Gerçek bir çalışan açıklamaya fiyat yazmaz; ai_uretimi ise MUAF
# (fazla detay vermek AI ayracıdır -- urun_detay_kopya ile aynı mantık).
# Yıllar (19xx/20xx) hariç tutulur: 'fuar 2026' meşru bir bağlam ifadesidir.
_TUTAR_SIZINTI = re.compile(
    r"\b(?!19\d{2}\b|20\d{2}\b)\d{3,}\b"   # 3+ haneli sayı (yıl değilse)
    r"|\b\d+[.,]\d{2}\b"                     # 1250,00 / 45.90
    r"|₺|\bTL\b|\blira\b",
    re.IGNORECASE,
)


# LATİN DIŞI ALFABE: sistem promptu "tamamen Türkçe" diyor ama denetlenmiyordu.
# qwen3_3 pilotunda bir çıktı Çince sızdırdı: "Vizyon'dan giyim类产品 aldım".
# Çok dilli modellerde nadir ama gerçek; tek karakter bile metni kullanılamaz kılar.
# CJK + Kiril + Arap/Fars + Yunan + İbrani + Hangul + Kana blokları.
_LATIN_DISI = re.compile(
    r"[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ"
    r"぀-ヿ㐀-䶿一-鿿가-힯]"
)


def _latin_disi_mi(metin: str) -> bool:
    return bool(_LATIN_DISI.search(metin))


def _tutar_sizinti_mi(metin: str) -> bool:
    return bool(_TUTAR_SIZINTI.search(metin))


def _urun_detay_kopya_mi(metin: str) -> bool:
    """Açıklamada ham ürün detayı (boyut/miktar/paket: '500Ml', '4 lu', '750 gr',
    '120w', 'pkt') geçiyor mu? İnsan kategorilerinde bu, çalışanın değil AI'ın
    ham kalem adını kopyaladığının işaretidir."""
    return bool(_URUN_DETAY_GURULTU.search(metin))


def _firma_icin_hatasi_mi(metin: str, fatura: dict) -> bool:
    """Firma adının hemen ardından 'için' gelmesi ('ABC için X aldık' gibi),
    doğru kaynak ekinin (-dan/-den/-tan/-ten) yerine yanlışlıkla amaç eki
    kullanıldığını gösterir (bkz. sistem prompt kuralı). Firma adı kısaltılmış
    hâliyle (firma_adi_kisalt) aranır, Türkçe-dayanıklı normalize ile (_tr_normalize)."""
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    if not firma_kisa:
        return False
    metin_norm = _tr_normalize(metin)
    firma_norm = _tr_normalize(firma_kisa)
    desen = re.escape(firma_norm) + r"\W{0,3}icin\b"
    return re.search(desen, metin_norm) is not None


def _firma_ek_hatasi_mi(metin: str, fatura: dict) -> bool:
    """Firma adına YANLIŞ ayrılma eki eklenmiş mi? ('Tanbay Karadeniz'dan' -> '-den').

    Ek Türkçede tamamen kurallı olduğu için doğrusu hesaplanabilir (ayrilma_eki);
    metinde firma adının hemen ardından DİĞER üç varyanttan biri geliyorsa ihlaldir.
    Yalnız ayrılma hâli denetlenir -- '-e/-de' gibi başka hâller meşru kullanımdır."""
    firma_kisa = firma_adi_kisalt(fatura["satici_unvan"])
    if not firma_kisa:
        return False
    dogru = ayrilma_eki(firma_kisa).rsplit("'", 1)[-1]        # dan/den/tan/ten
    yanlislar = {"dan", "den", "tan", "ten"} - {dogru}
    n = _tr_normalize(metin)
    firma_n = _tr_normalize(firma_kisa)
    return any(re.search(re.escape(firma_n) + r"['’]?" + y + r"\b", n) for y in yanlislar)


def _verbatim_kopya_mi(metin: str, kategori: str, dal: str | None = None) -> bool:
    """Üretilen metin, prompt'ta gösterilen FEW-SHOT örneklerinden birine neredeyse
    birebir mi? Stil demirleme yerine kopya -> çeşitlilik ölür.
    manipulatif'te prompt'a giren DAL havuzuna bakılır (gösterilmeyen örnekle
    karşılaştırıp yanlış pozitif üretmemek için).

    yetersiz'de eşik farklı: kural eskiden bu kategoriyi TAMAMEN muaf tutuyordu, çünkü
    kısa muğlak öbekler ('iş gideri') doğal olarak havuzla örtüşür. Ama muafiyet
    denetimsizlik demekti: qwen3 pilotunda 8 yetersizin 3'ü BİREBİR 'gerekliydi aldım
    işte' geldi ve 'kırtasiye alımı' havuz girdisinin aynısıydı. Tekrar bu kategoride
    DOĞALdır (bkz. CLAUDE.md §7) -- sorun tekrar değil, prompt'ta gösterdiğimiz 60
    stringin veri setine EZBERLENMESİ. Bu yüzden yalnız TAM eşleşme ihlal sayılır;
    yakın eşleşme serbest kalır."""
    if kategori == "yetersiz":
        n = _dedup_normalize(metin)
        if any(n == _dedup_normalize(o) for o in YETERSIZ_ORNEK_HAVUZ):
            return True
        # ...ve havuz girdisinin üstüne EN FAZLA 1 kelime eklenmiş hâli. Gözlenen
        # collapse ('gerekliydi aldım işte' ×3) tam eşleşme DEĞİLDİ: model iki havuz
        # girdisini birleştiriyordu. Jaccard eşiğini gevşetmek bu kategoride yanlış
        # pozitif patlatır (kısa muğlak öbekler doğal olarak örtüşür), bu yüzden ölçüt
        # dar: kapsama + en çok 1 fazla kelime.
        mt = _token_set(metin)
        return any(ot and ot <= mt and len(mt - ot) <= 1
                   for ot in (_token_set(o) for o in YETERSIZ_ORNEK_HAVUZ))
    havuz = _fewshot_havuz(kategori, dal)
    if not havuz:
        return False
    mt = _token_set(metin)
    return any(jaccard(mt, _token_set(o)) >= 0.9 for o in havuz)


def ihlalleri_bul(metin: str, kategori: str, fatura: dict, meta: dict | None = None) -> list[str]:
    meta = meta or {}
    ihlaller = []

    kalemler = fatura["kalemler"]

    if kategori == "manipulatif":
        # meta yoksa (geriye dönük çağrı) gizlenecek'i yeniden hesapla; ama meta
        # varsa onu kullan -- çünkü prompt_olustur gizleme dalını yalnızca gerçek
        # gizlenecek durumda seçer (temizde gizlenecek=None olur, sızıntı aranmaz).
        gizlenecek = meta["gizlenecek"] if "gizlenecek" in meta else gizlenecek_kalem_bul(kalemler)
        if gizlenecek and _sizinti_var_mi(metin, gizlenecek):
            ihlaller.append("sizinti")
        # "aşırı haklı çıkarma" dalında (meta["vurgu"] varsa) zorunlu vurgu var mı?
        if meta.get("vurgu") and not _vurgu_var_mi(metin, meta.get("vurgu")):
            ihlaller.append("vurgu_eksik")
        # ...ve tersi: ISRAR UST USTE BINMESIN (2026-07-30). meta["vurgu"] KOSULU
        # YOK -- bariz dalda vurgu zorunlu ama 'bir tane' demek gerekiyor, kurnaz/
        # gizleme/zorunluluk dallarinda ise zaten hic olmamali, ikisinde de
        # "en fazla 1" dogru sinir.
        elif _vurgu_sayisi(metin) > 1:
            ihlaller.append("vurgu_fazlasi")
        # Faz 2: kılıfı bozup ihlali itiraf/prosedür anlatma -> manipülatif niyeti bozar.
        if _karakter_kirilmasi_mi(metin):
            ihlaller.append("karakter_kirilmasi")

    # Pasif/resmi kalıp yasağı: Faz 2 ile manipulatif'e de genişletildi (pasif kalıp
    # manipülatifi ai_uretimi'ne benzetip sınıf sınırını bulanıklaştırıyordu).
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _pasif_ya_da_ucuncu_sahis_mi(metin):
        ihlaller.append("pasif_kalip")

    # ai_uretimi: havuzdaki HERHANGİ bir AI-kapanışıyla bitmesi yeter (tek belirli
    # olanı dayatmıyoruz -> gereksiz retry azalır, çeşitlilik prompt'tan gelir).
    if kategori == "ai_uretimi" and not _kapanis_var_mi_herhangi(metin):
        ihlaller.append("kapanis_eksik")

    # Faz 2: yeterli halüsinasyonu (fişte olmayan 'yemek/ağırlama' teması uydurma).
    if kategori == "yeterli" and _yeterli_halusinasyon_mi(metin, fatura):
        ihlaller.append("yeterli_halusinasyon")

    # `yetersiz`de tema halusinasyonu (2026-07-30). YALNIZ bu kategoride --
    # `yeterli`ye UYGULANMADI ve gerekcesi olculdu: yeterli'nin AMAC CUMLESI
    # mesru olarak baska temalara atif yapar ("sahada hijyen ihtiyaci icin sabun
    # aldim", "kiyafetleri duzenlemek icin"), kural "X aldim" ile "X ICIN aldim"i
    # ayiramiyor -> 200 kayitlik pilotta 7 flag'in 5'i YANLIS POZITIFTI (isabet
    # %29). `yetersiz`de amac cumlesi TANIMI GEREGI yok, o yuzden tema kelimesi
    # neredeyse her zaman iddia edilen bir alimdir -> isabet 1/1.
    # manipulatif'e de UYGULANMAZ: `gizleme` dalinin isi zaten kalemi anmadan
    # baska bir seyden bahsetmektir.
    if kategori == "yetersiz" and _tema_halusinasyonu_mi(metin, fatura):
        ihlaller.append("tema_halusinasyonu")

    # Faz 3: yeterli dayanak kontrolü (gerçek kalem/firma/iş-amacı çıpası yoksa muğlak).
    if kategori == "yeterli" and _yeterli_dayanaksiz_mi(metin, fatura):
        ihlaller.append("yeterli_dayanaksiz")

    # Faz 6: ürün-detay kopyası -- SADECE insan kategorileri (yeterli/yetersiz/
    # manipulatif). ai_uretimi serbest (ham ürün adını taşıması AI ayracıdır).
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _urun_detay_kopya_mi(metin):
        ihlaller.append("urun_detay_kopya")

    # Tutar/rakam sızıntısı -- aynı kapsam (insan kategorileri); ai_uretimi MUAF.
    if kategori in ("yeterli", "yetersiz", "manipulatif") and _tutar_sizinti_mi(metin):
        ihlaller.append("tutar_sizinti")

    # Faz 2: kategoriden BAĞIMSIZ denetimler (her kategori için).
    # Ham enum ('kisisel_bakim') her kategoride ihlal; BOŞLUKLU form ('kisisel bakim')
    # yalnız insan kategorilerinde (ai_uretimi'nde serbest -- AI ayracı).
    if _enum_sizinti_var_mi(metin, kalemler) or (
        kategori in ("yeterli", "yetersiz", "manipulatif")
        and _kategori_adi_sizinti_mi(metin, kalemler)
    ):
        ihlaller.append("enum_sizinti")
    if _meta_sizinti_var_mi(metin):
        ihlaller.append("meta_sizinti")
    if _latin_disi_mi(metin):
        ihlaller.append("latin_disi")
    if _verbatim_kopya_mi(metin, kategori, meta.get("dal")):
        ihlaller.append("verbatim_kopya")
    if _firma_icin_hatasi_mi(metin, fatura):
        ihlaller.append("firma_icin_hatasi")
    if _firma_ek_hatasi_mi(metin, fatura):
        ihlaller.append("firma_ek_hatasi")

    # Uzunluk: kategoriye özel SABİT sınır yerine, prompt_olustur'un bu faturaya
    # atadığı kategoriden-bağımsız uzunluk hedefine göre denetle (kategori-uzunluk
    # sahte korelasyonu kırılır). meta yoksa (geriye dönük çağrı) esnek bir aralık.
    uz = meta.get("uzunluk")
    if uz:
        _ad, uz_alt, uz_ust = uz
    else:
        uz_alt, uz_ust = (8 if kategori == "yetersiz" else 15), 230
    # yetersiz doğal olarak çok kısa olabilir (muğlaklık amaç) -> alt sınırı zorlama,
    # yalnız aşırı uzunu ele. Bu, iyi kısa yetersiz çıktılarındaki boşuna flag'i keser.
    if kategori == "yetersiz":
        uz_alt = 6
    # manipulatif'e ALT SINIR: pilotta 45 karakterlik bir "kurnaz" çıktı ('İş geliştirme
    # amaçlı yemek hizmeti kullanıldı') insan gözüyle YETERSİZ okunuyordu. O uzunlukta
    # şişirilmiş kurumsal kılıf kurulamıyor -> kategori sadakati düşüyor.
    elif kategori == "manipulatif":
        uz_alt = max(uz_alt, 55)
    # Üst sınıra tolerans: rastgele hedef ile modelin doğal uzunluğu arasındaki ufak
    # sapmalarda gereksiz retry olmasın (flag gürültüsünü azaltır). %15 -> %25:
    # `uzunluk` en sık tetiklenen ihlaldi ve retry'ların çoğu metni iyileştirmiyordu.
    uz_ust_tol = int(uz_ust * 1.25)
    if not (uz_alt <= len(metin) <= uz_ust_tol):
        ihlaller.append("uzunluk")

    return ihlaller


def duzeltme_notu_uret(ihlaller: list[str], meta: dict | None = None) -> str:
    meta = meta or {}
    notlar = []
    for i in ihlaller:
        if i not in DUZELTME_NOTLARI:
            continue
        if i == "kapanis_eksik":
            notlar.append(DUZELTME_NOTLARI[i].format(kapanis=meta.get("kapanis", "")))
        elif i == "vurgu_eksik":
            notlar.append(DUZELTME_NOTLARI[i].format(vurgu=meta.get("vurgu", "Kesinlikle")))
        elif i == "uzunluk":
            uz = meta.get("uzunluk")
            notlar.append(DUZELTME_NOTLARI[i].format(uzunluk_ad=uz[0] if uz else "kısa"))
        else:
            notlar.append(DUZELTME_NOTLARI[i])
    return "\n".join(notlar)


# ---------------------------------------------------------------------------
# VERBALIZED SAMPLING (Faz 4): collapse-eğilimli kategorilerde (yetersiz, manipulatif)
# tek çağrıda N farklı aday + olasılık üretip birini ÖRNEKLEYEREK mode collapse'ın
# kök nedeni "typicality bias"ı kır (bkz. arXiv:2510.01171). Argmax yerine olasılığa
# göre örnekleme, modeli en tipik tek moddan uzaklaştırır -> çeşitlilik ↑.
# ---------------------------------------------------------------------------

VS_KATEGORILER = {"yetersiz", "manipulatif"}

# VS DISI kategorilerde (yeterli / ai_uretimi) uretilecek ADAY sayisi.
#
# 2 -> 3 (2026-08-01). Gerekce faz-b-prompt.md 15: 8B kapasitesi doygun, her
# yeni kisit akiciligi yiyor. Retry'in yapisal sorunu da bu -- duzeltme notu
# prompt'a 8. bir kisit ekliyor. Kaggle pilotunda olculdu: retry %32, ikinci
# denemede de duzelmeyen %14.
#
# 3. aday NOTSUZ ve TAZE alinir (bagimsiz yeniden orneklem), boylece "daha cok
# kisit" yerine "daha cok deneme" stratejisine geciyoruz. Ihlalsiz aday cikar
# cikmaz DONULUR -- yani temiz uretimde ek maliyet YOK; 3. cagri yalniz ilk iki
# adayin da ihlalli oldugu ~%14'luk dilimde atilir.
ADAY_SAYISI = 3
VS_ADAY_SAYISI = 3

VS_SUFFIX = (
    "\n\nÖNEMLİ: Tek bir açıklama değil; yukarıdaki talimata TAM uyan, birbirinden "
    "belirgin biçimde FARKLI {n} açıklama üret. Her birine, o açıklamayı ne kadar olası/"
    "tipik bulduğunu gösteren 0 ile 1 arası bir olasılık ata. SADECE şu formatta yaz, "
    "her satırda bir aday, başka HİÇBİR şey ekleme:\n"
    "0.5 | birinci açıklama\n0.3 | ikinci açıklama\n0.2 | üçüncü açıklama"
)

# '0.5 | metin' / '.3: metin' / '1. 0.2 - metin' / '- 50% | metin' gibi biçimleri yakalar.
_VS_SATIR = re.compile(
    r"^\s*[-*•]?\s*(?:\d+[.\)]\s+)?[\(\[]?\s*(\d?\.?\d+)\s*%?\s*[\)\]]?\s*[|:\-–—]\s+(.+?)\s*$"
)


def _vs_ayristir(ham: str) -> list[tuple[float, str]]:
    """VS ham yanıtını (olasılık, metin) adaylarına ayrıştırır. Her adayın metni
    tek tek cikti_temizle'den geçer. Olasılık >1 ise yüzde kabul edilip /100 yapılır."""
    adaylar: list[tuple[float, str]] = []
    for satir in ham.splitlines():
        m = _VS_SATIR.match(satir)
        if not m:
            continue
        try:
            p = float(m.group(1))
        except ValueError:
            continue
        if p > 1:
            p = p / 100.0
        metin = cikti_temizle(m.group(2))
        if metin and len(metin) >= 3:
            adaylar.append((max(p, 0.0), metin))
    return adaylar


def _tek_fatura_vs(fatura, etiket, model, host, keep_alive, kategori,
                   system_prompt, user_prompt, meta, taban_sicaklik, min_p,
                   ) -> tuple[dict, dict, str | None, str | None, list[str], int]:
    """VS akışı: N aday üret -> ihlalsizleri süz -> olasılığa göre örnekle. Hiçbiri
    geçerli değilse düzeltme notuyla bir kez daha dener; yine olmazsa en iyi adayı
    (ihlalleriyle) döndürür. tek_fatura_isleme ile aynı 6'lı demeti döndürür."""
    vs_prompt = user_prompt + VS_SUFFIX.format(n=VS_ADAY_SAYISI)
    vs_token = 200 if kategori == "yetersiz" else 380
    en_iyi_metin = None
    en_iyi_ihl: list[str] | None = None

    for deneme in range(1, 3):
        sicaklik = taban_sicaklik if deneme == 1 else min(taban_sicaklik + 0.2, 1.3)
        seed = random.randint(1, 2**31 - 1)
        cagri_bilgi: dict = {}
        try:
            ham = ollama_cagir(
                system_prompt, vs_prompt, model, host,
                num_predict=vs_token, keep_alive=keep_alive, temperature=sicaklik,
                seed=seed, min_p=min_p, ham=True, num_ctx=1536, bilgi=cagri_bilgi,
            )
        except Exception as e:
            return fatura, etiket, None, str(e), [], deneme

        adaylar = _vs_ayristir(ham)
        # Token bütçesi bittiyse SON aday yarım kalmıştır (VS listesi sırayla üretilir).
        # Normal akışta bu `kesik` ihlali olur; burada aday zaten çoktur, atmak yeter.
        if cagri_bilgi.get("done_reason") == "length" and len(adaylar) > 1:
            adaylar = adaylar[:-1]
        if not adaylar:
            # VS formatı tutmadı -> tek metne indirge (graceful degrade, normal akış gibi)
            tek = cikti_temizle(ham)
            if tek:
                adaylar = [(1.0, tek)]

        gecerliler: list[tuple[float, str]] = []
        for p, t in adaylar:
            # Normal akışla aynı budama (yoksa VS kategorileri -- yetersiz/manipulatif --
            # uzunluk tavanından muaf kalırdı).
            t = uzunluk_buda(t, meta.get("uzunluk", (None, 0, 0))[2])
            ihl = ihlalleri_bul(t, kategori, fatura, meta)
            if _red_mi(t):
                ihl = ["red"] + ihl
            if not ihl:
                gecerliler.append((p, t))
            elif en_iyi_metin is None:
                en_iyi_metin, en_iyi_ihl = t, ihl

        if gecerliler:
            # Argmax DEĞİL: olasılığa göre örnekle (typicality bias'ı kır -> çeşitlilik).
            agirliklar = [max(p, 1e-3) for p, _ in gecerliler]
            secilen = random.choices([t for _, t in gecerliler], weights=agirliklar, k=1)[0]
            return fatura, etiket, secilen, None, [], deneme

        if deneme == 1 and en_iyi_ihl:
            vs_prompt = vs_prompt + "\n\n" + duzeltme_notu_uret(en_iyi_ihl, meta)

    if en_iyi_metin is None:
        return fatura, etiket, None, "VS: aday ayrıştırılamadı", [], 2
    return fatura, etiket, en_iyi_metin, None, en_iyi_ihl or [], 2


# ---------------------------------------------------------------------------
# ELEME KATMANI (2026-07-29) -- prompt'a DOKUNMADAN kaliteyi seçimde toplamak.
#
# ⚠️ ŞU AN BORU HATTINDA KAPALI. `aciklama_birlestir.py` bunu yalnız `--eleme`
#    bayrağıyla çağırır (varsayılan KAPALI). Kod ileride geliştirilmek üzere
#    duruyor; açmadan önce ELEME_IHLALLERI'ndeki enum_sizinti tartışması
#    çözülmeli (bkz. aciklama_birlestir.aciklama_haritasi_kur docstring'i).
#
# NEDEN AYRI: `ihlalleri_bul`'a yeni kural eklemek retry tetikler; ölçüldü
# (docs/arsiv/faz-b-prompt.md §15), her ek kural retry oranını yükseltip çeşitliliği
# düşürüyor ve 20k'da her 10 puan retry ≈ +8 saat. Buradaki kontroller ise
# üretimden SONRA çalışır: düzeltmeye çalışmaz, kaydı veri setinden ELER.
# Maliyeti sıfırdır (tek regex), riski de sıfırdır (prompt/retry davranışı
# değişmez).
#
# İKİ KAYNAK:
#   1) retry'dan SAĞ ÇIKAN sert ihlaller (`ELEME_IHLALLERI`) -- pilotta
#      enum_sizinti 4/32 (%12,5) retry'a rağmen düzelmedi.
#   2) `_kategori_kavrami_sizintisi_mi` -- enum_sizinti'nin YAKALAYAMADIĞI üst
#      biçim. Model kategori ADINI değil, kategori KAVRAMINI sızdırıyor:
#      "tavuk külbastı kategoriye giriyor". Bu cümle prompt'un YAPISINI
#      anlatıyor (kalemler `Ad (kategori)` biçiminde sunuluyor), harcamayı
#      değil. Hiçbir çalışan fişine "bu ürün şu kategoriye giriyor" yazmaz;
#      yazarsa bu, şema bilgisinin metne sızması = leakage'dır.
# ---------------------------------------------------------------------------

# Retry'dan sağ çıktığında kaydın ELENMESİNİ gerektiren ihlaller. Ölçüt:
# ihlal, etiketle korele bir ŞEMA/TALİMAT izi bırakıyor mu (leakage) --
# yoksa yalnızca üslup kusuru mu. Üslup kusurları (pasif_kalip, uzunluk)
# veri setinin doğal gürültüsüdür, ELENMEZ.
ELEME_IHLALLERI: frozenset[str] = frozenset({
    "meta_sizinti",     # görevi/karakteri anlatıyor (ground-truth sızıntısı)
    "sizinti",          # kategori/talimat sızıntısı
    "latin_disi",       # bozuk çıktı
    "red",              # model görevi reddetti
    "kesik",            # cümle ortadan kesilmiş
})

# Yalnız BELİRLİ kategorilerde eleyen ihlaller.
# `enum_sizinti` (sistem kategori adı metinde) `yeterli`/`manipulatif`te leakage
# DEĞİL: kalemin `harcama_kategorisi` alanı zaten model girdisinde, metnin "ofis
# mobilyası aldım" demesi modele yeni bilgi vermiyor ve cümle doğal Türkçe. O iki
# kategoride "ne için alındı" bilgisini gizlemek zaten istemiyoruz (§9).
# `yetersiz`te ise kategorinin TANIMINI bozuyor: baştan savma not ne aldığını
# söylememeli. Genel ad havuzu (`_KATEGORI_GENEL_AD`) bu kategoride zaten
# alternatif sunuyor ve 13 genel adın hiçbiri bu kuralı tetiklemiyor (ölçüldü),
# yani ihlal kalan vakalar ipucunun kullanılmadığı vakalardır.
ELEME_IHLALLERI_KATEGORILI: dict[str, frozenset[str]] = {
    "enum_sizinti": frozenset({"yetersiz"}),
}

# 'kategori' kelimesinin kendisi ve çekimli biçimleri. Doğal bir masraf notunda
# fiilen hiç geçmez (bu yüzden yanlış-pozitif riski ihmal edilebilir), ama
# prompt'taki `Ad (kategori)` biçimini anlatan model onu doğrudan kullanıyor.
_KATEGORI_KAVRAMI = re.compile(
    r"\b(?:kategori|kategoriye|kategorisi|kategorisine|kategorisinde|"
    r"kategorisinden|kategoride|kategoriler|kategorilerine|"
    r"harcama\s+kategori\w*|kalem\s+kategori\w*)\b"
)


def _kategori_kavrami_sizintisi_mi(metin: str) -> bool:
    """Metin, kategori KAVRAMINI (adını değil) anlatıyor mu?

    'tavuk külbastı kategoriye giriyor' -> True. `enum_sizinti` bunu kaçırır
    çünkü orada aranan şey kategori ADIdır ('yemek hizmeti'), burada geçen ise
    'kategori' kelimesinin kendisi."""
    return bool(_KATEGORI_KAVRAMI.search(_tr_normalize(metin)))


# Esi olmadan COZULEMEYEN anomaliler: fisin mukerrer oldugu kendi alanlarindan
# degil, esinin varligindan anlasilir.
ILISKISEL_ANOMALILER = ("mukerrer_fis_yukleme", "fatura_no_cakismasi")


def iliskisel_cift_idleri(faturalar: list[dict], etiket_map: dict[str, dict]) -> set[str]:
    """Iliskisel bir ciftte yer alan TUM kayit_id'ler: etiketli uye + esi.

    Eleme bu kumeye DOKUNMAMALI. Cift bir arada olmazsa anomali cozulemez hale
    gelir; birkac sema sizintili metni veri setinde tutmak, ilistkisel bir ornegi
    kaybetmekten ucuzdur (o ornek icin uretim zaten harcandi).

    Anahtar `(satici_vkn, fatura_no)`: yalniz `fatura_no` dogal cakismalari da
    cift sayardi (CLAUDE.md §7)."""
    anahtar: dict[tuple, list[str]] = {}
    for f in faturalar:
        anahtar.setdefault(cift_grup_anahtari(f), []).append(f["kayit_id"])
    korunan: set[str] = set()
    for f in faturalar:
        etiket = etiket_map.get(f["kayit_id"])
        if not etiket or not set(etiket["anomali_turleri"]) & set(ILISKISEL_ANOMALILER):
            continue
        korunan.update(anahtar[cift_grup_anahtari(f)])
    return korunan


def elenmeli_mi(metin: str, kalan_ihlaller: list[str] | None = None,
                kategori: str | None = None) -> tuple[bool, str]:
    """(elenmeli_mi, sebep). Sebep raporlamada gruplanır; elenmezse ("", ) döner.

    Boru hattında SON adımda çağrılır (aciklama_birlestir.py). Üretim/retry
    akışını etkilemez -- amaç düzeltmek değil, kötü kaydı veri setine
    SOKMAMAKTIR.

    `kategori` verilirse `ELEME_IHLALLERI_KATEGORILI` uygulanır; verilmezse
    kategori koşullu ihlaller HER kategoride eler (alan taşımayan eski çıktı
    dosyalarında davranış değişmesin diye)."""
    if not metin or not metin.strip():
        return True, "bos_metin"
    if _kategori_kavrami_sizintisi_mi(metin):
        return True, "kategori_kavrami"
    ihlaller = set(kalan_ihlaller or [])
    sert = ihlaller & ELEME_IHLALLERI
    for ihlal, kategoriler in ELEME_IHLALLERI_KATEGORILI.items():
        if ihlal in ihlaller and (kategori is None or kategori in kategoriler):
            sert.add(ihlal)
    if sert:
        return True, "ihlal:" + ",".join(sorted(sert))
    return False, ""


def tek_fatura_isleme(fatura, etiket, model, host, keep_alive: str | int | None = None,
                      ) -> tuple[dict, dict, str | None, str | None, list[str], int]:
    """`_tek_fatura_isleme_ham` + ÇIKTI DİZGİSİ normalizasyonu.

    Sarmalayıcı olmasının sebebi: ham fonksiyonun (VS akışı dâhil) BEŞ ayrı
    başarılı dönüş noktası var; dizgiyi her birinde tek tek uygulamak bir
    tanesini atlamaya davetiye çıkarır. Normalizasyon SON adımdır -- ihlal
    denetimi ham metin üzerinde yapılır, dizgi ondan sonra ayarlanır (dizgi
    hiçbir ihlal kuralını etkilemez: karşılaştırmalar `_dedup_normalize`
    üzerinden gider, o da küçük harfe çevirip noktalamayı atar)."""
    sonuc = _tek_fatura_isleme_ham(fatura, etiket, model, host, keep_alive)
    metin = sonuc[2]
    if not metin:
        return sonuc
    return (*sonuc[:2], dizgi_normalize(metin, etiket["aciklama_kategorisi"]), *sonuc[3:])


def _tek_fatura_isleme_ham(fatura, etiket, model, host, keep_alive: str | int | None = None,
                           ) -> tuple[dict, dict, str | None, str | None, list[str], int]:
    """
    Bir faturayı işler: prompt kur, Ollama'yı çağır, ihlal varsa bir kez
    düzeltici retry uygula. Dönüş:
        (fatura, etiket, metin, hata, kalan_ihlaller, deneme_sayisi)
    Retry aynı worker thread'inde çalışır; ThreadPoolExecutor sayesinde
    başka faturalar işlenirken paralel gerçekleşir.

    yetersiz/manipulatif için Verbalized Sampling akışına (çok-aday) dallanır.
    """
    kategori = etiket["aciklama_kategorisi"]
    # main.py:etiket_to_dict kararıyla girdide yok, prompt kurmak için kalemlere geri ekleniyor.
    fatura = {**fatura, "kalemler": [
        {**k, "harcama_kategorisi": kat}
        for k, kat in zip(fatura["kalemler"], etiket["harcama_kategorileri"])
    ]}
    system_prompt, user_prompt, meta = prompt_olustur(fatura, kategori, etiket.get("anomali_turleri"))
    # 2-cümleye açık kategorilerde (manipulatif/ai) truncation'ı önlemek için token
    # limiti yükseltildi; yetersiz kısa olduğu için düşük tutulur.
    # ai_uretimi 1-2 cümleye açık (paragraf değil) -> orta num_predict (140).
    # ai_uretimi 140 -> 220: meta detay + açılış/kapanış zorunluluğu birleşince 140 token
    # cümleyi ORTASINDAN kesiyordu (pilot #29). Uzunluk hedefi zaten ayrıca denetleniyor.
    # num_predict artık KATEGORİYE değil SEÇİLEN UZUNLUK HEDEFİNE bağlı. Sabit limit
    # iki yönden de yanlıştı: 'uzun' hedefi alan bir yeterli 100 token'da cümle
    # ORTASINDAN kesiliyordu (§15/4), 'çok kısa' hedefi alan üretim ise hiç fiziksel
    # fren görmüyordu (T1 pilotunda uzunluk 7/31 ihlalle en sık sorun). ~2 karakter/token
    # (Türkçe) + %50 pay: hedefe uyan metin RAHAT sığar, hedefi katlayan sığmaz.
    uz_ust_hedef = meta.get("uzunluk", (None, 0, 150))[2]
    token_limiti = int(uz_ust_hedef / 2.0) + 30
    if kategori == "ai_uretimi":
        # Zorunlu açılış (~25 krk) + kapanış (~30 krk) taban maliyeti var.
        token_limiti = max(token_limiti, 120)
    elif kategori == "yetersiz":
        # 'çok kısa' hedefi %39 oranında geliyor ve 52 token'lık bütçe modeli en güvenli
        # kalıba itiyordu (pilotta tek ifadeye çöktü). Taban 64: hedef yine kuralla
        # denetleniyor, sadece manevra alanı kalsın.
        token_limiti = max(token_limiti, 64)
    token_limiti = min(token_limiti, 260)
    # yeterli, fişteki gerçek kalemlere DAYANMALI -> düşük temp + düşük min_p uydurmayı
    # azaltır. Diğerleri çeşitlilik için yüksek temp'te (1.1) kalır, min_p 0.1 güvenlik ağı.
    taban_sicaklik = KATEGORI_SICAKLIK.get(kategori, 0.9)
    if SICAKLIK_TAVANI is not None:
        taban_sicaklik = min(taban_sicaklik, SICAKLIK_TAVANI)
    min_p = 0.05 if kategori == "yeterli" else 0.1

    # Faz 4: collapse-eğilimli kategoriler VS akışına gider.
    if kategori in VS_KATEGORILER:
        return _tek_fatura_vs(fatura, etiket, model, host, keep_alive, kategori,
                              system_prompt, user_prompt, meta, taban_sicaklik, min_p)

    temiz_user_prompt = user_prompt   # düzeltme notu EKLENMEMİŞ hâli (3. deneme için)
    metin = None
    ihlaller: list[str] = []
    # EN İYİ ADAYI TUT (2026-08-01). Eski akış iki denemenin SONUNCUSUNU
    # döndürüyordu: 1. deneme 1 ihlalliyse ve 2. deneme 3 ihlalli geldiyse
    # KÖTÜ olanı yazıyorduk. Artık en az ihlalli aday kazanır -- ek çağrı
    # maliyeti YOK, sadece hangi çıktının tutulacağı değişti.
    en_iyi: tuple[list[str], str] | None = None
    for deneme in range(1, ADAY_SAYISI + 1):
        # Sıcaklık merdiveni: retry'da temp'i biraz yükselt (takılan üretimden
        # çıkış) + her denemede farklı seed -> yeniden deneme gerçekten farklı.
        # min_p güvenlik ağı olduğu için tavan 1.3'e kadar açıldı.
        sicaklik = taban_sicaklik if deneme == 1 else min(taban_sicaklik + 0.2, 1.3)
        seed = random.randint(1, 2**31 - 1)
        cagri_bilgi: dict = {}
        try:
            metin = ollama_cagir(
                system_prompt, user_prompt, model, host,
                num_predict=token_limiti, keep_alive=keep_alive,
                temperature=sicaklik, seed=seed, min_p=min_p, stop=["\n\n"],
                bilgi=cagri_bilgi,
            )
        except Exception as e:
            return fatura, etiket, None, str(e), [], deneme

        # Hedefi aşan metni tam cümleden buda (retry'ın güvenilir düzeltemediği
        # tek ihlal uzunluktu). Denetim budanmış metin üzerinde yapılır.
        budanmis = uzunluk_buda(metin, meta.get("uzunluk", (None, 0, 0))[2])
        # Kesiklik BUDAMADAN ÖNCEKİ metne bakılır: budama zaten tam cümlede bittiği
        # için kesikliği gizlerdi. Budama devreye girdiyse artık kesik değildir.
        kesik = cagri_bilgi.get("done_reason") == "length" and budanmis == metin
        metin = budanmis
        ihlaller = ihlalleri_bul(metin, kategori, fatura, meta)
        if kesik:
            ihlaller = ["kesik"] + ihlaller
        # Red (moderasyon refleksi) kategori-bağımsız; kural ihlallerine ekle ki
        # düzeltme notuyla yeniden denensin.
        if _red_mi(metin):
            ihlaller = ["red"] + ihlaller
        if not ihlaller:
            return fatura, etiket, metin, None, [], deneme

        if en_iyi is None or len(ihlaller) < len(en_iyi[0]):
            en_iyi = (ihlaller, metin)

        if deneme == 1:
            # 2. deneme DÜZELTME NOTUYLA (mevcut davranış korunur).
            user_prompt = user_prompt + "\n\n" + duzeltme_notu_uret(ihlaller, meta)
        elif deneme == 2:
            # 3. deneme NOTSUZ ve TAZE: düzeltme notu prompt'a 8. bir kısıt
            # ekliyor, oysa 8B zaten doygun (faz-b-prompt.md 15) -- ikinci nota
            # daha fazla kısıt yığmak yerine bağımsız yeniden örneklem alıyoruz.
            user_prompt = temiz_user_prompt

    ihlaller, metin = en_iyi if en_iyi else (ihlaller, metin)
    return fatura, etiket, metin, None, ihlaller, ADAY_SAYISI
