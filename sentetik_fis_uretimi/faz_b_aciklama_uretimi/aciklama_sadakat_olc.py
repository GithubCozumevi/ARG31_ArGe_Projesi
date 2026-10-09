"""
LLM-JUDGE ile KATEGORİ SADAKATİ ölçümü (TEŞHİS, filtre DEĞİL).

NE YAPAR: temiz kümeden katmanlı bir örneklem çeker ve her metin için judge'a
"bu açıklama hedef kategoriye ne kadar sadık" diye sorar. Çıktı, `aciklama_analiz.py`
(distinct-n, ihlal frekansı) yanında duran İKİNCİ bir göstergedir.

⚠️ NEDEN "KALİTE" DEĞİL "SADAKAT" SORULUYOR: kitaplardaki standart judge prompt'u
("bu cevabı 0-100 arası puanla") BU VERİ SETİNDE YANLIŞTIR. İyi bir `yetersiz`
şudur: "gerekliydi aldım işte". Genel kalite soran her judge buna 20-30 verir;
puan eşiğiyle filtrelersen `yetersiz` ve `manipulatif` kategorilerini sistematik
olarak yok eder, `yeterli`/`ai_uretimi` ayakta kalır. Kalite = kategori sadakati
(CLAUDE.md §7), yazım güzelliği DEĞİL.

⚠️ NEDEN REFERANSSIZ: kitabın kodu `entry['output']` (gold answer) ile karşılaştırır.
Bizde gold answer YOK, üretim açık uçlu. O yüzden judge'a fiş + hedef kategori
tanımı verilir; elle küratörlü örnek hazırlamak GEREKMEZ.

⚠️ FİLTRE OLARAK KULLANMA: yüksek puanlıları tutmak "judge'ın beğendiği metin"
özelliğini etikete korele eder, yani yeni bir leakage vektörü açar (CLAUDE.md'deki
"pozitif-kelime skorunu HARD GATE yapma" uyarısının aynısı). Filtre zaten var ve
deterministik: `ihlalleri_bul` + yakın kopya.

GEÇERLİLİK DENETİMİ (--kontrol-orani): örneklemin bir kısmı KASITLI olarak YANLIŞ
kategoriyle sorulur (metin `yeterli`, judge'a `yetersiz` denir). Judge işe yarıyorsa
bu kontrol kayıtlarına belirgin biçimde DÜŞÜK puan vermeli. Aradaki fark küçükse
judge ölçmüyor demektir ve puanlarına güvenilmez -- rapor bunu açıkça yazar.

JUDGE MODELİ: üreten modelle aynı modeli judge yapmak öz-tercih yanlılığı taşır.
Mümkünse daha güçlü bir modele koştur (`--saglayici groq`). Sağlayıcı katmanı
`aciklama_uretim_core`'dan aynen kullanılır.

    # once ornegi gozle gor (LLM cagrisi YOK)
    python -m faz_b_aciklama_uretimi.aciklama_sadakat_olc --cikti-dizini data/ciktilar_25k-4 --kuru-calisma

    # gercek olcum
    python -m faz_b_aciklama_uretimi.aciklama_sadakat_olc --cikti-dizini data/ciktilar_25k-4 \
        --per-kategori 250 --cikti sadakat.jsonl
"""

import argparse
import glob
import json
import random
import re
import statistics
from collections import defaultdict
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import (
    MODEL_VARSAYILAN,
    OLLAMA_HOST_VARSAYILAN,
    kalemler_ozetle_prompt,
    ollama_cagir,
    saglayici_ayarla,
)

# Kategori tanımları ÜRETİM PROMPT'UNDAN BAĞIMSIZ yazıldı (kasıtlı): judge'a
# üretimdeki talimatın aynısını verirsek "talimata uydu mu"yu ölçeriz, oysa
# sorumuz "metin kategoriyi temsil ediyor mu". Tanımlar davranışı betimler,
# nasıl yazılacağını söylemez.
KATEGORI_TANIMLARI = {
    "yeterli": (
        "Çalışan harcamayı dürüstçe ve anlaşılır biçimde açıklamış: fişteki gerçek "
        "kalemle örtüşen, somut bir iş gerekçesi var. Yazım güzel olmak zorunda "
        "değil; gündelik ve özensiz de yazılmış olabilir."
    ),
    "yetersiz": (
        "Çalışan baştan savma yazmış: çok kısa, muğlak, gerekçesiz. Harcamanın neden "
        "yapıldığı anlaşılmıyor. BU KATEGORİDE muğlaklık KUSUR DEĞİL, kategorinin "
        "TANIMIDIR; 'genel gider', 'gerekliydi aldım' gibi metinler TAM İSABETTİR."
    ),
    "manipulatif": (
        "Çalışan harcamayı olduğundan daha meşru göstermeye çalışıyor: şişirilmiş "
        "kurumsal kılıf, aşırı ısrarlı haklı çıkarma, gerçek kalemi gizleme ya da "
        "kaçınılmazlık savunması. Amaç onaylatmak."
    ),
    "ai_uretimi": (
        "Metin bir yapay zekâya yazdırılmış gibi: aşırı resmi, kalıplaşmış, edilgen "
        "ve kişiliksiz kurumsal dil ('İşbu masraf ... tanzim edilmiştir'). Gerçek bir "
        "çalışanın gündelik sesi yok."
    ),
}

SYSTEM_PROMPT = (
    "Sen bir veri kalitesi denetçisisin. Sana bir masraf fişi, bir HEDEF KATEGORİ "
    "tanımı ve bir çalışan açıklaması verilecek. Görevin, açıklamanın hedef "
    "kategoriyi ne kadar iyi temsil ettiğini puanlamak.\n"
    "ÖNEMLİ: metnin ne kadar 'iyi yazılmış' olduğunu DEĞİL, hedef kategoriye ne "
    "kadar UYDUĞUNU puanla. Bazı kategoriler kasıtlı olarak kötü, muğlak ya da "
    "yanıltıcı metin gerektirir; böyle bir kategoride kötü görünen metin YÜKSEK "
    "puan alır.\n"
    "Yalnızca 1 ile 5 arasında tek bir tam sayı yaz, başka hiçbir şey yazma.\n"
    "5 = kategoriyi tam temsil ediyor, 3 = kısmen, 1 = hiç uymuyor."
)


def kullanici_prompt(fatura: dict, kategori: str, metin: str) -> str:
    return (
        f"FİŞ\n"
        f"  Satıcı: {fatura['satici_unvan']}\n"
        f"  Kalemler: {kalemler_ozetle_prompt(fatura['kalemler'])}\n\n"
        f"HEDEF KATEGORİ: {kategori}\n"
        f"  Tanım: {KATEGORI_TANIMLARI[kategori]}\n\n"
        f"ÇALIŞAN AÇIKLAMASI:\n  {metin}\n\n"
        f"Bu açıklama hedef kategoriyi ne kadar temsil ediyor? (1-5, sadece sayı)"
    )


def _puan_ayikla(ham: str) -> int | None:
    """Yanıttan ilk 1-5 arası tam sayıyı çeker. Judge geveze olursa da çalışır."""
    m = re.search(r"\b([1-5])\b", ham or "")
    return int(m.group(1)) if m else None


def ornek_sec(cikti_dizini: str, faturalar: dict, per_kategori: int,
              kontrol_orani: float, seed: int) -> list[dict]:
    """Kategori başına katmanlı örneklem + geçerlilik kontrolü kayıtları."""
    havuz: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for yol in sorted(glob.glob(str(Path(cikti_dizini) / "batch_*_ciktilar.json"))):
        for kid, v in json.loads(Path(yol).read_text(encoding="utf-8")).items():
            if v["kalan_ihlaller"] or v.get("yakin_kopya") or kid not in faturalar:
                continue
            havuz[v["aciklama_kategorisi"]].append((kid, v["aciklama_metni"]))

    rnd = random.Random(seed)
    ornekler: list[dict] = []
    for kategori, kayitlar in sorted(havuz.items()):
        secim = rnd.sample(kayitlar, min(per_kategori, len(kayitlar)))
        n_kontrol = int(len(secim) * kontrol_orani)
        # Kontrol kayıtları: metin AYNI kalır, judge'a YANLIŞ kategori söylenir.
        yanlis_adaylar = [k for k in KATEGORI_TANIMLARI if k != kategori]
        for i, (kid, metin) in enumerate(secim):
            kontrol = i < n_kontrol
            ornekler.append({
                "kayit_id": kid,
                "gercek_kategori": kategori,
                "sorulan_kategori": rnd.choice(yanlis_adaylar) if kontrol else kategori,
                "kontrol": kontrol,
                "metin": metin,
            })
    rnd.shuffle(ornekler)
    return ornekler


def raporla(sonuclar: list[dict]) -> None:
    gercek = [s for s in sonuclar if not s["kontrol"] and s["puan"] is not None]
    kontrol = [s for s in sonuclar if s["kontrol"] and s["puan"] is not None]

    print("\n" + "=" * 62)
    print("  KATEGORİ SADAKATİ (1-5, yüksek = kategoriye daha sadık)")
    print("=" * 62)
    print(f"\n{'kategori':<14}{'n':>6}{'ortalama':>10}{'medyan':>8}   dağılım(1..5)")
    kat = defaultdict(list)
    for s in gercek:
        kat[s["gercek_kategori"]].append(s["puan"])
    for k in sorted(kat):
        p = kat[k]
        dag = " ".join(f"{p.count(i):>4}" for i in range(1, 6))
        print(f"{k:<14}{len(p):>6}{statistics.mean(p):>10.2f}"
              f"{statistics.median(p):>8.1f}   {dag}")

    if gercek:
        print(f"\nGENEL ORTALAMA: {statistics.mean([s['puan'] for s in gercek]):.2f} / 5")

    print("\n--- GEÇERLİLİK DENETİMİ (kasıtlı yanlış kategori) ---")
    if not kontrol:
        print("  kontrol kaydı yok (--kontrol-orani 0)")
        return
    g = statistics.mean([s["puan"] for s in gercek]) if gercek else 0.0
    k = statistics.mean([s["puan"] for s in kontrol])
    print(f"  doğru kategori : {g:.2f}   (n={len(gercek)})")
    print(f"  yanlış kategori: {k:.2f}   (n={len(kontrol)})")
    print(f"  FARK           : {g - k:+.2f}")
    if g - k < 0.8:
        print("\n  ⚠️  Fark küçük. Judge kategori sadakatini AYIRT EDEMİYOR;")
        print("      yukarıdaki puanlara güvenme, judge modelini büyüt.")
    else:
        print("\n  ✓ Judge doğru ile yanlış kategoriyi ayırt ediyor.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cikti-dizini", default="data/ciktilar_25k-4")
    ap.add_argument("--faturalar", default="data/faturalar.json")
    ap.add_argument("--per-kategori", type=int, default=250)
    ap.add_argument("--kontrol-orani", type=float, default=0.15,
                    help="örneklemin kaçta kaçı kasıtlı yanlış kategoriyle sorulsun")
    ap.add_argument("--model", default=MODEL_VARSAYILAN)
    ap.add_argument("--host", default=None)
    ap.add_argument("--saglayici", default="ollama", choices=["ollama", "groq", "vllm"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--cikti", default=None, help="JSONL kayıt dosyası")
    ap.add_argument("--kuru-calisma", action="store_true",
                    help="LLM çağırmadan örneklemi ve ilk prompt'u göster")
    a = ap.parse_args()

    faturalar = {f["kayit_id"]: f
                 for f in json.loads(Path(a.faturalar).read_text(encoding="utf-8"))}
    ornekler = ornek_sec(a.cikti_dizini, faturalar, a.per_kategori,
                         a.kontrol_orani, a.seed)
    n_kontrol = sum(1 for o in ornekler if o["kontrol"])
    print(f"örneklem: {len(ornekler)} (kontrol: {n_kontrol})")
    dagilim = defaultdict(int)
    for o in ornekler:
        dagilim[o["gercek_kategori"]] += 1
    print("kategori dağılımı:", dict(sorted(dagilim.items())))

    if a.kuru_calisma:
        o = ornekler[0]
        print("\n--- ÖRNEK PROMPT ---")
        print(kullanici_prompt(faturalar[o["kayit_id"]], o["sorulan_kategori"], o["metin"]))
        print("\n(LLM çağrılmadı; gerçek ölçüm için --kuru-calisma'yı kaldır)")
        return

    # saglayici_ayarla HER ZAMAN çağrılmalı: modül düzeyinde _SAGLAYICI'yı kurar.
    # `a.host or saglayici_ayarla(...)` yazmak, --host verildiğinde sağlayıcıyı
    # kurmadan bırakır ve groq/vllm sessizce Ollama gövdesine düşerdi.
    varsayilan_host = saglayici_ayarla(a.saglayici, host=a.host)
    host = a.host or varsayilan_host or OLLAMA_HOST_VARSAYILAN
    sonuclar: list[dict] = []
    for i, o in enumerate(ornekler, 1):
        ham = ollama_cagir(
            SYSTEM_PROMPT,
            kullanici_prompt(faturalar[o["kayit_id"]], o["sorulan_kategori"], o["metin"]),
            a.model, host,
            num_predict=8,          # tek rakam bekliyoruz
            temperature=0.0,        # judge DETERMİNİSTİK olmalı
            seed=a.seed, min_p=0.0, num_ctx=1536,
        )
        sonuclar.append({**o, "ham_yanit": ham, "puan": _puan_ayikla(ham)})
        if i % 100 == 0:
            print(f"  [{i}/{len(ornekler)}]")

    okunamayan = sum(1 for s in sonuclar if s["puan"] is None)
    if okunamayan:
        print(f"\nUYARI: {okunamayan} yanıttan puan çıkarılamadı.")

    if a.cikti:
        with open(a.cikti, "w", encoding="utf-8") as f:
            for s in sonuclar:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        print(f"kayıt -> {a.cikti}")

    raporla(sonuclar)


if __name__ == "__main__":
    main()
