"""
GENEL ARAÇ (tek seferlik DEĞİL -- final_veriler her değiştiğinde tekrar
çalıştırılır, bkz. commit.txt): docs/raporlar/kalite_raporu.html'i
data/final_veriler'in GÜNCEL hâlinden yeniden üretir.

Rapor 2026-08-11'de `data/faturalar_aciklamali.json` (24.132 kayıt) üzerinden
donmuştu. O tarihten sonra: giyim/ara_toplam kayıtları silindi (24.132 ->
21.564), 360 kayıtlık limit_asimi+ondalik birleştirmesi `aciklama_metni` VE
`aciklama_kategorisi`'ni değiştirdi. Bu script final_veriler'in girdi+etiket
çiftini birleştirip (harcama_kategorisi etikette, `ihlalleri_bul` için gerekli)
TÜM istatistikleri yeniden hesaplar.

BİLİNÇLİ EKSİK: "1./2./3. deneme" (retry) dağılımı ve "yakin_kopya" sayısı
ÜRETİM ANI meta verisidir (runner'ın batch çıktı JSON'unda dursurdu), final
metinden geriye türetilemez -- bu script onları RAPORLAMAZ, şablondan da
kaldırır (sessizce sıfır göstermek yanıltıcı olurdu). "Kalan ihlal" (retry
sonrası hâlâ duran kural ihlalleri) ise `ihlalleri_bul`'u nihai aciklama_metni
üzerinde YENİDEN ÇALIŞTIRARAK doğru biçimde yeniden üretilebilir -- ki bu
zaten şipin veri setindeki gerçek kalite göstergesidir (deneme sayısından
daha önemli).

Kullanım (repo kökünden):
    python dataset_quality_reports/final_veriler_kalite_raporu_html_uret.py
"""

import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faz_b_aciklama_uretimi.aciklama_uretim_core import KATEGORILER, distinct_n, _dedup_normalize, ihlalleri_bul

FINAL_DIZIN = Path("data/final_veriler")
HTML_YOLU = Path("docs/raporlar/kalite_raporu.html")
BOLMELER = ["egitim", "dogrulama", "test"]
ONAYLAR = ["onaylandi", "gozden_gecirilecek", "onaylanmadi"]
SEED = 42


def kayitlari_yukle() -> list[dict]:
    kayitlar = []
    for b in BOLMELER:
        girdi = json.loads((FINAL_DIZIN / f"{b}_girdi.json").read_text(encoding="utf-8"))
        etiket = {e["kayit_id"]: e for e in json.loads((FINAL_DIZIN / f"{b}_etiket.json").read_text(encoding="utf-8"))}
        for g in girdi:
            e = etiket[g["kayit_id"]]
            rec = dict(g)
            rec["aciklama_kategorisi"] = e["aciklama_kategorisi"]
            rec["onay_durumu"] = e["onay_durumu"]
            rec["anomali_turleri"] = e["anomali_turleri"]
            rec["is_anomali"] = e["is_anomali"]
            rec["kalemler"] = [{**k, "harcama_kategorisi": hk}
                                for k, hk in zip(g["kalemler"], e["harcama_kategorileri"])]
            kayitlar.append(rec)
    return kayitlar


def uzunluk_istatistik(metinler: list[str]) -> dict:
    uzunluklar = sorted(len(m) for m in metinler)
    n = len(uzunluklar)
    kelimeler = sum(len(m.split()) for m in metinler) / n
    return {
        "n": n, "ort_krk": round(sum(uzunluklar) / n, 1), "med_krk": uzunluklar[n // 2],
        "min_krk": uzunluklar[0], "max_krk": uzunluklar[-1], "ort_kel": round(kelimeler, 1),
    }


def histogram(metinler: list[str]) -> dict:
    h: Counter = Counter()
    for m in metinler:
        h[(len(m) // 20) * 20] += 1
    return {str(k): v for k, v in sorted(h.items())}


def main() -> None:
    kayitlar = kayitlari_yukle()
    K = list(KATEGORILER)
    by_kat = {k: [r for r in kayitlar if r["aciklama_kategorisi"] == k] for k in K}

    ozet, esit, dizgi, hist = {}, {}, {}, {}
    for k in K:
        metinler = [r["aciklama_metni"] for r in by_kat[k]]
        normlar = [_dedup_normalize(m) for m in metinler]
        u = uzunluk_istatistik(metinler)
        ozet[k] = {**u, "d1": round(distinct_n(metinler, 1), 4), "d2": round(distinct_n(metinler, 2), 4),
                   "benzersiz": len(set(normlar))}
        buyuk = sum(1 for m in metinler if m and m[0].isupper())
        nokta = sum(1 for m in metinler if m.rstrip().endswith("."))
        dizgi[k] = {"buyuk": round(buyuk / u["n"], 4), "nokta": round(nokta / u["n"], 4), "n": u["n"]}
        hist[k] = histogram(metinler)

    esit_n = min(ozet[k]["n"] for k in K)
    rnd = random.Random(SEED)
    for k in K:
        metinler = [r["aciklama_metni"] for r in by_kat[k]]
        ornek = rnd.sample(metinler, esit_n)
        esit[k] = {"n": esit_n, "d1": round(distinct_n(ornek, 1), 4), "d2": round(distinct_n(ornek, 2), 4)}

    norm_gruplar: dict[str, list[dict]] = {}
    for r in kayitlar:
        norm_gruplar.setdefault(_dedup_normalize(r["aciklama_metni"]), []).append(r)
    benzersiz_oran = round(len(norm_gruplar) / len(kayitlar), 4)
    en_sik = sorted(norm_gruplar.items(), key=lambda kv: -len(kv[1]))[:10]
    collapse = [
        {"metin": grup[0]["aciklama_metni"], "adet": len(grup),
         "kategori": Counter(r["aciklama_kategorisi"] for r in grup).most_common(1)[0][0]}
        for _, grup in en_sik
    ]

    capraz = {k: dict(Counter(r["onay_durumu"] for r in by_kat[k])) for k in K}
    for k in K:
        for o in ONAYLAR:
            capraz[k].setdefault(o, 0)

    tum_turler = sorted({t for r in kayitlar for t in r["anomali_turleri"]},
                         key=lambda t: -sum(1 for r in kayitlar if t in r["anomali_turleri"]))
    anomali_kategori = {
        t: {k: sum(1 for r in by_kat[k] if t in r["anomali_turleri"]) for k in K}
        for t in tum_turler
    }

    ihlal_sayaci: Counter = Counter()
    kat_ihlal: dict[str, list[int]] = {k: [0, len(by_kat[k])] for k in K}
    for r in kayitlar:
        ihlaller = ihlalleri_bul(r["aciklama_metni"], r["aciklama_kategorisi"], r)
        for i in ihlaller:
            ihlal_sayaci[i] += 1
        if ihlaller:
            kat_ihlal[r["aciklama_kategorisi"]][0] += 1

    D = {
        "ozet": ozet, "esit": esit, "dizgi": dizgi, "hist": hist, "collapse": collapse,
        "benzersiz_oran": benzersiz_oran, "capraz": capraz, "anomali_kategori": anomali_kategori,
        "uretim": {"n": len(kayitlar), "ihlal": dict(ihlal_sayaci.most_common()), "kat_ihlal": kat_ihlal},
        "kats": K,
    }

    html = HTML_YOLU.read_text(encoding="utf-8")
    html = re.sub(r"const D = \{.*\};", "const D = " + json.dumps(D, ensure_ascii=False) + ";", html)
    html = html.replace(
        "24.132 LLM üretimi açıklama metninin",
        f"{len(kayitlar):,}".replace(",", ".") + " LLM üretimi açıklama metninin",
    )
    html = html.replace(
        "<span>data/faturalar_aciklamali.json</span>",
        "<span>data/final_veriler</span>",
    )

    # "Üretim" bölümü artık deneme/yakin_kopya İÇERMİYOR (final veriden türetilemez,
    # bkz. modül docstring'i) -- eyebrow'daki retry sayacını ve üç deneme kartını kaldır,
    # yalnız (yeniden hesaplanabilen) kalan ihlalli kayıt kartı kalır.
    html = html.replace(
        '<p class="eyebrow">Üretim &middot; <span id="retryOran"></span> retry</p>',
        '<p class="eyebrow">Üretim</p>',
    )
    html = html.replace(
        '<p class="note">Her metin en fazla üç aday üretir, en az ihlalli olan kazanır.\n'
        '  &ldquo;Kalan ihlal&rdquo; üçüncü adaydan sonra da geçmeyen kuraldır.</p>',
        '<p class="note">Deneme sayısı ve yakın-kopya oranı üretim anının meta verisidir, '
        'final veri setinde saklanmaz -- burada yeniden üretilemiyor. &ldquo;Kalan '
        'ihlal&rdquo; ise nihai <code>aciklama_metni</code> üzerinde <code>ihlalleri_bul</code> '
        'yeniden çalıştırılarak hesaplanır: ship edilen veri setindeki gerçek kalan kusur budur.</p>',
    )
    html = html.replace(
        "const U = D.uretim, tot = U.n;\n"
        "const retry = (U.deneme['2']||0) + (U.deneme['3']||0);\n"
        "document.getElementById('retryOran').textContent = pf(retry/tot,1);\n"
        "const ihlalliToplam = Object.values(U.kat_ihlal).reduce((a,b) => a + b[0], 0);\n"
        "document.getElementById('uretimCards').innerHTML = [\n"
        "  ['1. denemede geçti', nf(U.deneme['1']), pf(U.deneme['1']/tot,1) + ' (hiç retry yok)'],\n"
        "  ['2. deneme', nf(U.deneme['2']), pf(U.deneme['2']/tot,1) + ' (düzeltme notuyla)'],\n"
        "  ['3. deneme', nf(U.deneme['3']), pf(U.deneme['3']/tot,1) + ' (notsuz ve taze aday)'],\n"
        "  ['Kalan ihlalli kayıt', nf(ihlalliToplam), pf(ihlalliToplam/tot,1) + ' (üç adaydan sonra da geçmeyen)']\n"
        "].map(([k,v,n]) => '<div class=\"card\"><div class=\"nm\">' + k + '</div><span class=\"big\">' + v +\n"
        "  '</span><dl><dt style=\"grid-column:1/-1;text-align:left\">' + n + '</dt></dl></div>').join('');",
        "const U = D.uretim, tot = U.n;\n"
        "const ihlalliToplam = Object.values(U.kat_ihlal).reduce((a,b) => a + b[0], 0);\n"
        "document.getElementById('uretimCards').innerHTML = [\n"
        "  ['Kalan ihlalli kayıt', nf(ihlalliToplam), pf(ihlalliToplam/tot,1) + ' (final metinde en az bir kural ihlali)']\n"
        "].map(([k,v,n]) => '<div class=\"card\"><div class=\"nm\">' + k + '</div><span class=\"big\">' + v +\n"
        "  '</span><dl><dt style=\"grid-column:1/-1;text-align:left\">' + n + '</dt></dl></div>').join('');",
    )
    html = re.sub(
        r"<footer>aciklama_uretim_core\.py.*?</footer>",
        "<footer>aciklama_uretim_core.py &middot; 2026-08-19</footer>",
        html,
    )

    HTML_YOLU.write_text(html, encoding="utf-8")
    print(f"[+] {HTML_YOLU} güncellendi -- {len(kayitlar)} kayıt")
    for k in K:
        print(f"    {k:12s} n={ozet[k]['n']:6d}  d1={ozet[k]['d1']:.4f}  benzersiz={ozet[k]['benzersiz']}")


if __name__ == "__main__":
    main()
