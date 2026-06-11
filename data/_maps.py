# -*- coding: utf-8 -*-
"""data/_maps.py — детерминированные карты нормализации для тегирования новостей (MASTER_SPEC П1).

Цель — привести разнородные обозначения языка и страны из GDELT (полные английские имена:
"English", "United States") и NewsAPI.ai/EventRegistry (ISO 639-3: "eng"; иногда location.country)
к единому виду: язык → ISO 639-1 (2 буквы), страна → ISO 3166-1 alpha-2 (2 буквы).

Карты покрывают практически весь языковой охват GDELT и ~150 наиболее частых стран.
Неизвестное значение → None: тег остаётся пустым, но без ручной правки (П1) — лучше «нет данных»,
чем выдумка (П8). Расширяется добавлением строк, не правкой данных.
"""

# --- Язык: ISO 639-3 (NewsAPI.ai) → ISO 639-1 -------------------------------
ISO3_TO_ISO1 = {
    "eng": "en", "spa": "es", "rus": "ru", "ara": "ar", "zho": "zh", "cmn": "zh",
    "fra": "fr", "deu": "de", "ger": "de", "por": "pt", "ita": "it", "nld": "nl",
    "dut": "nl", "pol": "pl", "tur": "tr", "jpn": "ja", "kor": "ko", "ukr": "uk",
    "ron": "ro", "rum": "ro", "ell": "el", "gre": "el", "swe": "sv", "ces": "cs",
    "cze": "cs", "hun": "hu", "fin": "fi", "dan": "da", "nor": "no", "nob": "no",
    "nno": "no", "heb": "he", "hin": "hi", "tha": "th", "vie": "vi", "ind": "id",
    "msa": "ms", "may": "ms", "fas": "fa", "per": "fa", "urd": "ur", "ben": "bn",
    "tam": "ta", "tel": "te", "mar": "mr", "guj": "gu", "kan": "kn", "mal": "ml",
    "pan": "pa", "bul": "bg", "hrv": "hr", "srp": "sr", "slk": "sk", "slv": "sl",
    "lit": "lt", "lav": "lv", "est": "et", "cat": "ca", "glg": "gl", "eus": "eu",
    "baq": "eu", "isl": "is", "ice": "is", "gle": "ga", "sqi": "sq", "alb": "sq",
    "mkd": "mk", "mac": "mk", "bel": "be", "kat": "ka", "geo": "ka", "hye": "hy",
    "arm": "hy", "aze": "az", "kaz": "kk", "uzb": "uz", "swa": "sw", "amh": "am",
    "som": "so", "yor": "yo", "hau": "ha", "ibo": "ig", "zul": "zu", "afr": "af",
    "tgl": "tl", "fil": "tl", "mya": "my", "bur": "my", "khm": "km", "lao": "lo",
    "sin": "si", "nep": "ne", "pus": "ps", "kur": "ku", "snd": "sd",
}

# --- Язык: полное английское имя (GDELT) → ISO 639-1 ------------------------
NAME_TO_ISO1 = {
    "english": "en", "spanish": "es", "russian": "ru", "arabic": "ar",
    "chinese": "zh", "mandarin chinese": "zh", "french": "fr", "german": "de",
    "portuguese": "pt", "italian": "it", "dutch": "nl", "polish": "pl",
    "turkish": "tr", "japanese": "ja", "korean": "ko", "ukrainian": "uk",
    "romanian": "ro", "greek": "el", "swedish": "sv", "czech": "cs",
    "hungarian": "hu", "finnish": "fi", "danish": "da", "norwegian": "no",
    "hebrew": "he", "hindi": "hi", "thai": "th", "vietnamese": "vi",
    "indonesian": "id", "malay": "ms", "persian": "fa", "farsi": "fa",
    "urdu": "ur", "bengali": "bn", "tamil": "ta", "telugu": "te", "marathi": "mr",
    "gujarati": "gu", "kannada": "kn", "malayalam": "ml", "punjabi": "pa",
    "bulgarian": "bg", "croatian": "hr", "serbian": "sr", "slovak": "sk",
    "slovenian": "sl", "lithuanian": "lt", "latvian": "lv", "estonian": "et",
    "catalan": "ca", "galician": "gl", "basque": "eu", "icelandic": "is",
    "irish": "ga", "albanian": "sq", "macedonian": "mk", "belarusian": "be",
    "georgian": "ka", "armenian": "hy", "azerbaijani": "az", "kazakh": "kk",
    "uzbek": "uz", "swahili": "sw", "amharic": "am", "somali": "so",
    "yoruba": "yo", "hausa": "ha", "igbo": "ig", "zulu": "zu", "afrikaans": "af",
    "tagalog": "tl", "filipino": "tl", "burmese": "my", "myanmar": "my",
    "khmer": "km", "lao": "lo", "sinhala": "si", "sinhalese": "si",
    "nepali": "ne", "pashto": "ps", "kurdish": "ku", "sindhi": "sd",
}

# --- Страна: полное английское имя → ISO 3166-1 alpha-2 ----------------------
# GDELT отдаёт sourcecountry полным именем; NewsAPI.ai location.country.label.eng — тоже.
COUNTRY_NAME_TO_ISO2 = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "russia": "RU", "russian federation": "RU", "china": "CN",
    "people's republic of china": "CN", "germany": "DE", "france": "FR",
    "spain": "ES", "italy": "IT", "portugal": "PT", "netherlands": "NL",
    "belgium": "BE", "switzerland": "CH", "austria": "AT", "poland": "PL",
    "ukraine": "UA", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK",
    "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR",
    "croatia": "HR", "serbia": "RS", "slovenia": "SI", "bosnia and herzegovina": "BA",
    "north macedonia": "MK", "macedonia": "MK", "albania": "AL", "montenegro": "ME",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "iceland": "IS", "ireland": "IE", "estonia": "EE", "latvia": "LV",
    "lithuania": "LT", "belarus": "BY", "moldova": "MD", "luxembourg": "LU",
    "malta": "MT", "cyprus": "CY",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "venezuela": "VE",
    "ecuador": "EC", "bolivia": "BO", "paraguay": "PY", "uruguay": "UY",
    "cuba": "CU", "dominican republic": "DO", "guatemala": "GT", "panama": "PA",
    "costa rica": "CR", "honduras": "HN", "el salvador": "SV", "nicaragua": "NI",
    "japan": "JP", "south korea": "KR", "korea": "KR", "republic of korea": "KR",
    "north korea": "KP", "india": "IN", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "nepal": "NP", "afghanistan": "AF", "myanmar": "MM",
    "burma": "MM", "thailand": "TH", "vietnam": "VN", "cambodia": "KH",
    "laos": "LA", "malaysia": "MY", "singapore": "SG", "indonesia": "ID",
    "philippines": "PH", "taiwan": "TW", "hong kong": "HK", "macau": "MO",
    "mongolia": "MN", "kazakhstan": "KZ", "uzbekistan": "UZ", "turkmenistan": "TM",
    "kyrgyzstan": "KG", "tajikistan": "TJ",
    "turkey": "TR", "iran": "IR", "iraq": "IQ", "saudi arabia": "SA",
    "united arab emirates": "AE", "uae": "AE", "qatar": "QA", "kuwait": "KW",
    "bahrain": "BH", "oman": "OM", "yemen": "YE", "israel": "IL",
    "palestine": "PS", "jordan": "JO", "lebanon": "LB", "syria": "SY",
    "egypt": "EG", "libya": "LY", "tunisia": "TN", "algeria": "DZ",
    "morocco": "MA", "sudan": "SD", "south sudan": "SS",
    "nigeria": "NG", "ghana": "GH", "kenya": "KE", "ethiopia": "ET",
    "tanzania": "TZ", "uganda": "UG", "south africa": "ZA", "angola": "AO",
    "mozambique": "MZ", "zimbabwe": "ZW", "zambia": "ZM", "cameroon": "CM",
    "ivory coast": "CI", "cote d'ivoire": "CI", "senegal": "SN", "mali": "ML",
    "democratic republic of the congo": "CD", "congo": "CG", "rwanda": "RW",
    "botswana": "BW", "namibia": "NA", "madagascar": "MG", "tunisa": "TN",
    "australia": "AU", "new zealand": "NZ", "fiji": "FJ", "papua new guinea": "PG",
}

# Полные имена → 2-буквенный код мы зовём «alpha-2»; FIPS не используем,
# GDELT DOC API отдаёт sourcecountry именем, а не FIPS-кодом.
