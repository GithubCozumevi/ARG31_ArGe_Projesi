# Kaggle'da Toplu Açıklama Üretimi (Runbook)

> 25 bin açıklamayı ücretsiz GPU üzerinde üretmek için adım adım aşamalar.
> Faz B'nin üretim adımını çalıştırır; öncesi ve sonrası
> `02-faz-b-aciklama-uretimi.md`'de.

## Neden Kaggle?

Ücretsiz LLM API kotaları bu ölçek için yetersiz. Çağrı başına ölçülen maliyet
~1.065 token; günlük 100 bin token veren bir servisle 25 bin kayıt aylar
sürer. Kaggle token değil **GPU saati** ölçer: haftada 30 saat, oturum başına
en fazla 12 saat, iki adet T4. Bu, ücretsiz kalarak 8 milyar parametreli bir
modeli kendi sunucunda çalıştırmanın tek yolu.

Ölçülen tempo: **~66 kayıt/dakika** (Qwen3-8B fp16, 16 worker). 25 bin kayıt
yaklaşık **6,5 saat**.

## 1. Paketi hazırla (yerelde)

Yüklenecekler: 25 batch dosyası, `durum.json` ve **dört** Python dosyası
(`aciklama_uretim_core.py`, `aciklama_toplu_uret.py`, `aciklama_analiz.py`,
`cift_grup.py`). `cift_grup.py`'yi atlama: `aciklama_uretim_core` onu import
ediyor, yoksa üretim hücresi daha ilk satırda `ModuleNotFoundError` verir.
`faturalar.json` ve CSV havuzları **gerekmez** — paket ~4,5 MB kalır.

> **2026-08-26'da klasör yapısı değişti:** bu dört dosya artık flat değil,
> `faz_b_aciklama_uretimi/aciklama_uretim_core.py` + `faz_b_aciklama_uretimi/aciklama_toplu_uret.py` +
> `faz_b_aciklama_uretimi/aciklama_analiz.py` ve `ortak/cift_grup.py` altında yaşıyor.
> Paket bu alt klasör yapısını (her ikisinde `__init__.py` ile) KORUYARAK
> yüklenmeli — Kaggle notebook'u artık `python -m faz_b_aciklama_uretimi.aciklama_toplu_uret`
> ile `/kaggle/working` kökünden çalışıyor (flat `python aciklama_toplu_uret.py`
> DEĞİL). Ayrıntı: notebook'un 3. hücresi.

```bash
mkdir -p /tmp/paket/kaggle_v10/aciklama_25k
cp data/aciklama_25k/batch_[0-9][0-9][0-9][0-9].json \
   data/aciklama_25k/durum.json /tmp/paket/kaggle_v10/aciklama_25k/
mkdir -p /tmp/paket/kaggle_v10/faz_b_aciklama_uretimi /tmp/paket/kaggle_v10/ortak
cp faz_b_aciklama_uretimi/aciklama_uretim_core.py faz_b_aciklama_uretimi/aciklama_toplu_uret.py faz_b_aciklama_uretimi/aciklama_analiz.py \
   /tmp/paket/kaggle_v10/faz_b_aciklama_uretimi/
cp ortak/cift_grup.py /tmp/paket/kaggle_v10/ortak/
touch /tmp/paket/kaggle_v10/faz_b_aciklama_uretimi/__init__.py /tmp/paket/kaggle_v10/ortak/__init__.py
(cd /tmp/paket && zip -qr ~/kaggle_v10_yukleme.zip kaggle_v10)
```

Kaggle'a **yeni dataset** olarak yükle, notebook'a ekle ve **eski sürümü Input
panelinden çıkar.**

## 2. Notebook ayarları

Sağ panelden: Accelerator **GPU T4 ×2**, Internet **açık**, Persistence
kapalı olabilir.

## 3. Hücreler

> Aşağıdaki sayılar 25 batch / 25.000 kayıtlık koşuya göre. Kendi koşunda dört
> yeri değiştir: Hücre 3'te `assert len(b) == 25`, Hücre 5'te `--batch 1-25` ve
> `--ilerleme 1000`, Hücre 6'da `"/ 25000"`. `--ilerleme` batch boyutundan büyük
> kalırsa hücre hiç ilerleme basmaz ve çalışıp çalışmadığını göremezsin.

**Hücre 1 — kurulum**

```python
!pip install -q vllm hf_transfer
!nvidia-smi --query-gpu=name,memory.total --format=csv
```

**Hücre 2 — ortam**

```python
import os
os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
try:
    from kaggle_secrets import UserSecretsClient
    os.environ["HF_TOKEN"] = UserSecretsClient().get_secret("HF_TOKEN")
except Exception as e:
    print("HF_TOKEN yok, anonim indirme:", e)
```

**Hücre 3 — dosyaları kopyala ve doğrula.** Bu hücrenin assert'leri bilerek
serttir; her biri daha önce yaşanmış bir hatadan geliyor:

```python
import shutil, glob, os, json

adaylar = [os.path.dirname(os.path.dirname(p)) for p in
           glob.glob("/kaggle/input/**/faz_b_aciklama_uretimi/aciklama_uretim_core.py", recursive=True)]
assert adaylar, "dataset notebook'a ekli mi? (faz_b_aciklama_uretimi/ altinda degil mi?)"
assert len(adaylar) == 1, f"BIRDEN COK kod kaynagi: {adaylar} - eskisini cikar"
KAYNAK, HEDEF = adaylar[0], "/kaggle/working/aciklama_25k"
os.makedirs(HEDEF, exist_ok=True)

for f in glob.glob(f"{KAYNAK}/**/batch_[0-9][0-9][0-9][0-9].json", recursive=True) + \
         glob.glob(f"{KAYNAK}/**/durum.json", recursive=True):
    shutil.copy(f, HEDEF)
# faz_b_aciklama_uretimi/ + ortak/ alt klasor yapisi KORUNARAK kopyalanir (flat degil)
for alt in ("faz_b_aciklama_uretimi", "ortak"):
    shutil.copytree(f"{KAYNAK}/{alt}", f"/kaggle/working/{alt}", dirs_exist_ok=True)

b = sorted(glob.glob(f"{HEDEF}/batch_[0-9][0-9][0-9][0-9].json"))
print("batch:", len(b), "| cikti:", len(glob.glob(f"{HEDEF}/*_ciktilar.json")))
assert len(b) == 25
```

**Hücre 4 — vLLM sunucusu.** Model indirmesi 10-20 dakika sürer ve bu sırada
hücre hiçbir şey basmaz; donmuş değildir. Log `vllm.log`'a gider.

```python
import subprocess, time, requests
MODEL = "Qwen/Qwen3-8B"
log = open("/kaggle/working/vllm.log", "w")
sunucu = subprocess.Popen(
    ["python", "-m", "vllm.entrypoints.openai.api_server", "--model", MODEL,
     "--dtype", "half", "--tensor-parallel-size", "2", "--max-model-len", "2048",
     "--gpu-memory-utilization", "0.92", "--enable-prefix-caching",
     "--generation-config", "vllm", "--enforce-eager", "--port", "8000"],
    stdout=log, stderr=subprocess.STDOUT)

for i in range(360):
    if sunucu.poll() is not None:
        print("SUNUCU OLDU, exit =", sunucu.returncode); break
    try:
        if MODEL in str(requests.get("http://localhost:8000/v1/models", timeout=5).json()):
            print("HAZIR, sn =", i*10); break
    except Exception: pass
    time.sleep(10)

r = requests.post("http://localhost:8000/v1/chat/completions",
                  json={"model": MODEL, "max_tokens": 16,
                        "messages": [{"role": "user", "content": "Merhaba de."}]}, timeout=180)
print("duman testi:", r.status_code)
```

Duman testinin çıktısında `<think>` görmek **normaldir**: bu ham bir istektir.
Üretim çağrıları çekirdekten geçer ve orada düşünme kapatılır.

**Hücre 5 — üretim.** `2>&1` **yazma**: fatura başına satır log dosyasına
gitsin, ilerleme satırları hücrede canlı görünsün.

```python
%%time
!cd /kaggle/working && python -u -m faz_b_aciklama_uretimi.aciklama_toplu_uret \
    --cikti-dizini /kaggle/working/aciklama_25k \
    --saglayici vllm --host http://localhost:8000/v1 \
    --model Qwen/Qwen3-8B \
    --sicaklik-tavani 0.9 --cooldown-min 0 --workers 16 \
    --ilerleme 1000 --batch 1-25 > /kaggle/working/uretim.log
!tail -40 /kaggle/working/uretim.log
```

Beklenen çıktı:

```
[ilerleme] 1000 kayit | 15 dk | 65 kayit/dk | retry 299
[ilerleme] 2000 kayit | 31 dk | 65 kayit/dk | retry 594
```

Retry oranı %29-31 bandında olmalı ve **koşu boyunca sabit kalmalı**; tırmanması
modelin bozulduğuna işarettir.

**Hücre 6 — özet ve paketleme.** try/except ile sarılıdır: üretim yarım kalsa
bile hücre patlamaz.

```python
import json, glob, collections, zipfile, os
dosyalar = sorted(glob.glob("/kaggle/working/aciklama_25k/*_ciktilar.json"))
toplam, ih = 0, collections.Counter()
for f in dosyalar:
    C = json.load(open(f)); toplam += len(C)
    for v in C.values():
        for x in (v.get("kalan_ihlaller") or []): ih[x] += 1
print("uretilen:", toplam, "/ 25000"); print("kalan ihlaller:", dict(ih.most_common(12)))

with zipfile.ZipFile("/kaggle/working/ciktilar_25k.zip", "w", zipfile.ZIP_DEFLATED) as z:
    for f in dosyalar: z.write(f, os.path.basename(f))

!cd /kaggle/working && python -m faz_b_aciklama_uretimi.aciklama_analiz --cikti-dizini /kaggle/working/aciklama_25k
```

## 4. İndirme ve derleme

`ciktilar_25k.zip`'i indir, batch dosyalarının yanına aç, sonra yerelde:

```bash
unzip -o ~/Downloads/ciktilar_25k.zip -d data/aciklama_25k/
python -m faz_b_aciklama_uretimi.aciklama_birlestir --cikti-dizini data/aciklama_25k --sadece-uretilenler
python -m faz_b_aciklama_uretimi.onay_durumu_ata    --cikti-dizini data/aciklama_25k
```

## 5. Öğrenilenler — tekrar düşmemek için

Bunların hepsi bir koşuya mal oldu:

- **Jupyter tek çekirdeklidir.** Bir hücre çalışırken tıkladığın diğer hücre
  başlamaz, **kuyruğa girer**; yanındaki dönen simge "çalışıyorum" değil
  "sıramı bekliyorum" demektir. Koşu sürerken yedek almak istiyorsan üretimi
  kesmen gerekir (kayıp olmaz, resume çalışır).

- **Çalışan hücrenin yıldızına tıklama** — interrupt gönderir.

- **Hücrenin gerçekten başladığını doğrula.** Bir koşuda üretim hücresi
  kuyruğa hiç girmemiş ve GPU dört saat boş beklemişti. İlk `[ilerleme] 1000`
  satırını gör, öyle bırak.

- **Oturum boşta kalınca sonlanır ve `/kaggle/working` silinir.** Uzun koşuda
  ara ara Hücre 6'yı çalıştırıp zip'i indir. Gözetimsiz bırakacaksan
  **Save Version → Save & Run All (commit)** kullan: tarayıcıdan bağımsız
  çalışır, çıktı versiyon olarak saklanır.

- **Kota GPU'ya bağlı duvar saatini sayar**, hesap yapıp yapmadığını değil. Boş
  bekleyen oturum da kotadan yer. İş bitince oturumu kapat.

- **`repetition_penalty` gönderme.** Adı Ollama'daki ile aynı, semantiği
  farklı: vLLM prompt token'larını da cezalandırır. 1.15 ile ~1000 token'lik
  Türkçe prompt'u cezalandırmak modeli tam da kullanması gereken kelimelerden
  uzaklaştırdı (latin dışı karakter 5 → 18, uzunluk ihlali 46 → 75). `min_p`
  iki tarafta aynı semantiktedir, o kalır.

- **Kesinti veri kaybettirmez.** Runner her fatura bittiğinde diske yazar;
  aynı komut kaldığı yerden devam eder.
