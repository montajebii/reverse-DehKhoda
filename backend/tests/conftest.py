"""Shared pytest fixtures. Tests are hermetic: a synthetic MySQL dump with
real Dehkhoda rows is built in tmp, and the hash embedder avoids any model
download. Real-model semantic tests run only when DEHKHODA_RUN_MODEL_TESTS=1.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
from pathlib import Path

import pytest

from app.config import Settings
from app.vector_store.qdrant_backend import QdrantBackend

# (id, word, cleaned meaning) — real Dehkhoda-SQL rows (truncated)
SAMPLE_ENTRIES = [
    (11, "آب", "آب . (اِ) (اوستائی آپ ap، سانسکریت آپ َ apa، پارسی باستانی آپی api، پهلوی آپ ap) مایعی شفاف بی مَزه و بوی که حیوان از آن آشامد و نبات بدان تازگی و تری گیرد. و آن یکی از چهار عنصر قدماست و به عربی آن را ماء و بلال خوانند."),
    (1810, "آفتاب", "آفتاب . (اِ مرکب ) (از: آف، مِهر، خور + تاب، فروغ، نور) نور شمس . خورشید. مقابل سایه : شخصی نه چنان کریه منظر کز زشتی او خبر توان داد. سعدی . (اِخ ) توسعاً، بزرگترین کوکب آسمان زمین که هر صبح طالع شود و روی زمین روشنی دهد."),
    (83867, "جان", "جان . [ جان ن ] (ع ص ) پوشاننده . تاریک کننده . ساتر. (اِ) ج ِ جِن ّ. (اقرب الموارد). اسم جمع جن چنانکه جامل و باقر: لم یطمثهن انس قبلهم و لاجان . (قرآن ٥٦/٥٥). (از تاج العروس ). مقابل انس . پریان ."),
    (114394, "خورشید", "خورشید. [ خُرْ ] (اِخ ) شهرکیست در سواحل فارس و در کنار خلیجی که نزدیک به یک فرسخ با دریا فاصله دارد و کشتیها بدانجارفت و آمد میکنند و بازار بین سینیز و سیراف است ."),
    (124038, "دل", "دل . [ دَل ل ] (ع مص ) ناز نمودن زن بر شوهر خود. (از منتهی الارب ). ناز کردن . (تاج المصادر بیهقی ) (دهار). جرأت نشان دادن زن بر شوهر خویش با غنج و ناز، گویی که با او مخالفت می کند در حالی که قصد مخالفت ندارد."),
    (150810, "ستاره", "ستاره . [ س ِ رَ / رِ ] (اِ مرکب ) طنبوره و سازی را گویند که سه تار داشته باشد و به این معنی منفصل باید نوشت . (برهان ) (جهانگیری ). از: سه + تار + َه (پسوند نسبت ) = دارای سه سیم . نام ساز که آن را ستار گویند. (غیاث )."),
    (160922, "شب", "شب . [ ش ُب ب ] (اِخ ) نام موضعی است دریمن . (منتهی الارب ) (از متن اللغة) (ناظم الاطباء). شب: روبروی روز. از غروب آفتاب تا طلوع خورشید. تاریکی زمانه ."),
    (183618, "عشق", "عشق . [ ع َ ش َ ] (ع مص ) عشق آوردن و چیره گردیدن دوستی بر کسی . (از منتهی الارب ). عاشق شدن . (المصادر زوزنی ). نیک شگفت داشتن . (تاج المصادر بیهقی ). تعلق قلب به کسی . (از اقرب الموارد). چسبیدن . التصاق به چیزی ."),
    (284181, "چشم", "چشم . [ چ ِ ش ُ ] (اِخ ) دهی از دهستان کاهبخش داورزن شهرستان سبزوار که ٤٠هزارگزی جنوب خاوری داورزن و ٢٢هزارگزی جنوب راه شوسه ٔ عمومی سبزوار واقع است . جلگه و معتدل است و ٧٩٢ تن سکنه دارد."),
    (291884, "کتاب", "کتاب . [ ک ِ ] (ع مص ) کَتب . کِتابَة. (منتهی الارب ) (اقرب الموارد) (متن اللغة). نبشتن و یقال کتبت بالقلم . (از منتهی الارب ). نگاشتن لفظ به حروف هجاء در چیزی، مانند خط. (اقرب الموارد). فرمان راندن ."),
    # --- reverse-dictionary acceptance entries (real Dehkhoda rows) -----------
    (156563, "سودا", "سودا. [ س َ ] (ع ص ) سیاه . (غیاث اللغات ) (آنندراج ). ♦ سودا بر سر زدن ؛ مرادف زیر کردن سیاهی . (آنندراج ). رجوع به سوداء شود. ◄ (اِ) خرید و فروخت . (غیاث اللغات ). خرید و فروخت که دو کس با هم کنند و این ترکی است . (آنندراج ). تجارت . بیع و شری . معامله . داد و ستد."),
    (272346, "وسواس", "وسواس . [ وَس ْ ] (ع اِ) اندیشه ٔ بد و آنچه در دل گذرد. (منتهی الارب ) (آنندراج ) (ناظم الاطباء). هر بدی که به قلب خطور کند وهر چیزی که در آن خیر و صلاح نباشد. (اقرب الموارد). عبارت است از خواطر نفسانیه ٔ جسمانیه، خواه عقلی باشد خواه حسی باشد. آنچه شیطان در دل افکند. (ناظم الاطباء). آنچه شیطان در دل انسان افکند و او را به کار بد برانگیزد. (فرهنگ فارسی معین )."),
    (272347, "وسواس", "وسواس . [ وِس ْ ] (ع مص ) بد اندیشیدن . (منتهی الارب ). بد اندیشیدن و اغوا کردن شیطان . (از ناظم الاطباء). در دل افکندن شیطان و نفس چیزی بی نفع و بی خیر. (ناظم الاطباء) (منتهی الارب ) (از اقرب الموارد). وسوسه . (منتهی الارب ). قوله تعالی : فوسوس لهما الشیطان . (قرآن ٢٠/٧). سخن گفتن به کلامی آهسته و تکرار کردن آن . (اقرب الموارد)."),
    (34714, "اضطراب", "اضطراب . [ اِ طِ ] (ع مص ) اضطراب چیزی ؛ تحرک و موج زدن و برخی از آن برخوردن یا زدن به برخی است . (از اقرب الموارد). تحرک و موج زدن . (از لسان العرب ). جنبیدن و حرکت نمودن . (منتهی الارب ). جنبیدن و لرزیدن و طپیدن . (آنندراج ). سخت جنبان شدن . (تاج المصادر بیهقی ). تپیدن . طپش . اهتزاز. حرکت . (لسان العرب ). ◄ اضطراب بحر و نحو آن ؛ موج زدن دریا و مانند آن . (ناظم الاطباء)."),
    (263501, "نگرانی", "نگرانی . [ ن ِ گ َ ] (حامص ) بینندگی . (فرهنگ فارسی معین ). نگران و ناظر بودن. ◄ انتظار. چشم داشت . (از ناظم الاطباء). ترصد. ◄ اضطراب . دلواپسی .(ناظم الاطباء). تشویش . (فرهنگ فارسی معین ). ◄ توقف . تأمل . (فرهنگ فارسی معین ). ◄ بصیرت . فراست . (از ناظم الاطباء)."),
]

RUN_MODEL_TESTS = os.environ.get("DEHKHODA_RUN_MODEL_TESTS") == "1"


def make_dump(path: Path, entries=SAMPLE_ENTRIES) -> Path:
    """Write a phpMyAdmin-style .sql.gz containing the sample entries."""
    rows = ",\n".join(
        "({id}, '{w}', '{m}')".format(id=i, w=w.replace("'", "''"), m=m.replace("'", "''"))
        for i, w, m in entries
    )
    sql = (
        "-- phpMyAdmin SQL Dump\n"
        "CREATE TABLE `words` (\n"
        "  `id` int(255) NOT NULL,\n"
        "  `word` text NOT NULL,\n"
        "  `meaning` text NOT NULL\n"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;\n"
        "INSERT INTO `words` (`id`, `word`, `meaning`) VALUES\n" + rows + ";\n"
        "ALTER TABLE `words` ADD PRIMARY KEY (`id`);\n"
    )
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(sql)
    return path


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.backend = "qdrant"
    s.embedder = "hash"
    s.dense_dim = 256
    s.data_dir = tmp_path / "data"
    s.qdrant_mode = "local"
    s.qdrant_path = tmp_path / "qdrant"
    s.collection_name = "dehkhoda_test"
    s.batch_size = 1000
    return s


@pytest.fixture()
def analyzer():
    from app.analyzers.persian import PersianAnalyzer

    return PersianAnalyzer(os.environ.get("DEHKHODA_ANALYZER_ENGINE", "hazm"))


@pytest.fixture()
def qdrant_backend(settings: Settings):
    b = QdrantBackend(settings)
    b.connect()
    yield b
    b.close()


@pytest.fixture()
def sample_dump(tmp_path: Path) -> Path:
    return make_dump(tmp_path / "01-Test.sql.gz")


@pytest.fixture()
def staging_db(tmp_path: Path, sample_dump: Path) -> Path:
    """SQLite staging built from the synthetic dump (mimics loader.import_to_staging)."""
    from app.analyzers.persian import headword_key
    from app.ingestion.mysql_dump_parser import clean_html, iter_dump, make_document

    db = tmp_path / "staging.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE words (id INTEGER PRIMARY KEY, word TEXT NOT NULL,"
        " meaning_clean TEXT NOT NULL, doc TEXT NOT NULL, word_norm TEXT)"
    )
    for e in iter_dump(sample_dump):
        cleaned = clean_html(e.meaning)
        con.execute(
            "INSERT INTO words VALUES (?,?,?,?,?)",
            (e.id, e.word, cleaned, make_document(e.word, cleaned), headword_key(e.word)),
        )
    con.commit()
    con.execute("CREATE INDEX idx_words_norm ON words(word_norm)")
    con.commit()
    con.close()
    return db
