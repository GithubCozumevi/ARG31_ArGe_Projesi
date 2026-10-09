# masraf-denetim-infra

Masraf Denetim projesinin yerel altyapisi: fis gorsellerini ve model dosyalarini saklayan **MinIO** ve deney takibi yapan **MLflow** (Postgres veritabani ile). Hepsi Docker ile calisir.

```
Python kodu  --->  MLflow (5000)  --->  Postgres (deney/metrik kayitlari)
                        |
                        +------------->  MinIO (9000)  (model ve artifact dosyalari)
Python kodu  ------------------------->  MinIO (9000)  (fis gorselleri, etiketler)
```

## Klasor yapisi

| Yol | Ne ise yarar |
| --- | --- |
| `minio/docker-compose.yml` | MinIO servisi ve `shared-infra` Docker agi |
| `mlflow/docker-compose.yml` | MLflow sunucusu + Postgres (MinIO'nun agina katilir) |
| `test_mlflow_minio.py` | MLflow -> MinIO baglantisini deneyen kucuk test |
| `toplu_yukle.py` | Bir klasordeki fis gorsellerini ve etiket JSON'larini MinIO'ya yukler |

## Gereksinimler

- Docker Desktop (`docker compose` komutu calismali)
- Python 3.10+ ve `pip install mlflow boto3 minio python-dotenv`

## Kurulum

### 1. `.env` dosyalarini olustur

Sifreler repoda **yoktur**; her `.env` dosyasini kendi bilgisayarinda olusturursun. `.env` dosyalari `.gitignore` icindedir, git'e eklenmez.

`minio/.env`
```
MINIO_ROOT_USER=<kullanici-adi>
MINIO_ROOT_PASSWORD=<guclu-sifre>
```

`mlflow/.env`
```
POSTGRES_USER=<kullanici-adi>
POSTGRES_PASSWORD=<guclu-sifre>
POSTGRES_DB=<veritabani-adi>
MLFLOW_BUCKET_NAME=<bucket-adi>
MINIO_ACCESS_KEY=<adim 3'te olusturulacak>
MINIO_SECRET_KEY=<adim 3'te olusturulacak>
```

MinIO ve Postgres icin farkli sifreler kullan.

### 2. MinIO'yu baslat (ilk bu)

```
cd minio
docker compose up -d
```

Bu komut `shared-infra` agini da olusturur. MLflow bu aga katilir, bu yuzden once MinIO baslatilmalidir.

### 3. MinIO'da bucket ve access key olustur

1. Tarayicida `http://localhost:9001` ac, `minio/.env` icindeki root kullanici ve sifre ile giris yap.
2. **Buckets** bolumunde `mlflow/.env` icindeki `MLFLOW_BUCKET_NAME` adiyla bir bucket olustur.
3. **Access Keys** bolumunde yeni bir key olustur (root kullanici/sifre degil, ayri bir key). Cikan `Access Key` ve `Secret Key` degerlerini `mlflow/.env` dosyasina yaz. Secret Key sadece olusturulurken gosterilir.

### 4. MLflow'u baslat

```
cd ../mlflow
docker compose up -d
```

Arayuz: `http://localhost:5000`

### 5. Baglantiyi test et

Proje kokunde:
```
python test_mlflow_minio.py
```

Basarili olursa MLflow arayuzunde `test-baglanti` deneyinde bir run ve MinIO'da kaydedilmis `test_dosyasi.txt` gorunur. (Script `mlflow/.env` icindeki MinIO key'lerini okur.)

## Fis gorsellerini MinIO'ya yuklemek

`toplu_yukle.py` dosyasinin ustundeki `KAYNAK_KLASORLER`, `MINIO_ACCESS_KEY` ve `MINIO_SECRET_KEY` degerlerini kendine gore duzenle (anahtari dosyaya kalici olarak yazip git'e gondermemeye dikkat et), sonra:
```
python toplu_yukle.py
```

Gorseller `masraf-fisler`, JSON etiketleri `masraf-etiketler` bucket'ina, her klasor adiyla (`gercek/`, `sentetik/`) bir alt yol olarak yuklenir. Bucket'lar yoksa script olusturur.

## Servisleri durdurmak

```
cd mlflow && docker compose down
cd ../minio && docker compose down
```

Veriler Docker volume'lerinde (`minio_data`, `mlflow_db_data`) kalir. Verileri de silmek icin `docker compose down -v` kullan; bu islem geri alinamaz.

## Portlar

| Servis | Port | Aciklama |
| --- | --- | --- |
| MinIO API | 9000 | Kodun S3 uyumlu baglandigi adres |
| MinIO Console | 9001 | Tarayici arayuzu |
| MLflow | 5000 | Deney takip arayuzu (sifresiz) |

## Guvenlik notlari

- Bu kurulum **yerel gelistirme** icindir. MLflow arayuzunde kimlik dogrulama yoktur; ortak bir sunucuya ayni hali ile koyma.
- `.env` dosyalari ve access key'ler repoya girmez; kodda sifre yazma. Bir sifre yanlislikla commit edilirse sifreyi hemen degistir.
- Gercek fis gorselleri ve etiketleri repoya yuklenmez, yalnizca MinIO'da tutulur.

## Sik karsilasilan sorunlar

- **`network shared-infra declared as external, but could not be found`**: MinIO'yu (adim 2) baslatmadan MLflow'u baslatmissin. Once MinIO'yu baslat.
- **MLflow artifact yukleyemiyor / `NoSuchBucket`**: `MLFLOW_BUCKET_NAME` adli bucket MinIO'da yok ya da `mlflow/.env` icindeki access key yanlis.
- **`docker compose up` sonrasi servis kapaniyor**: `docker compose logs <servis-adi>` ile hatayi gor. En sik neden `.env` dosyasinin eksik ya da bos olmasidir.
