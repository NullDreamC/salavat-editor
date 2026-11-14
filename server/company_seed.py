from .domain import audit_row, utc_now
from .knowledge import _snapshot


COMPANY_ID = 1
COMPANY_NAME = "Салаватстекло"
EXCERPT_URL = "https://salstek.ru/"
EXCERPT_LABEL = "Сведения из предоставленной пользователем выжимки salstek.ru от 2026-11-14"
EXCERPT = (
    "АО «Салаватстекло» — один из крупнейших производителей листового стекла в РФ. "
    "Производства в Саратовской области, Республике Башкортостан и Дагестане. "
    "День рождения Салаватского завода — 30 июня 1962 года.\n\n"
    "Over Size: летом 2024 реконструкция линии нанесения покрытий; особо прозрачное стекло "
    "с мультифункциональным магнетронным покрытием формата Over Size. "
    "В июле 2022 выпущено первое листовое Over Size размером 12000 × 3210 мм.\n\n"
    "Ultraview R: листовое стекло с пониженным содержанием железа, повышенное светопропускание, "
    "архитектура и интерьер, нейтральный цвет даже при увеличенных толщинах.\n\n"
    "Clearview: антибликовое стекло методом магнетронного напыления в вакуумной установке, "
    "покрытие снижает блики и отражения; применение: витрины, музеи, зоопарки, ложи стадионов, "
    "смотровые площадки, автосалоны, ювелирные витрины, фасады.\n\n"
    "Профсоюзная организация работников АО «Салаватстекло» является юридическим лицом с 1993 года. "
    "Действующий состав профсоюзного комитета избран в октябре 2014 года. "
    "На данный момент в профсоюзе предприятия состоит 1228 человек.\n\n"
    "На страже здоровья работников стоит фельдшерский здравпункт, который работает ежедневно "
    "с 07:30 до 21:00 (без перерыва и выходных). Главная задача фельдшеров — оказание экстренной "
    "и неотложной помощи при травмах, внезапных заболеваниях, отравлениях и тяжелых состояниях."
)

ENTRIES = (
    (
        "АО «Салаватстекло» и география производств", "business", "active",
        "АО «Салаватстекло» производит листовое стекло. Предприятия группы расположены в Саратовской области, "
        "Республике Башкортостан и Республике Дагестан.",
    ),
    (
        "Дата основания Салаватского завода", "business", "active",
        "30 июня 1962 года — день рождения Салаватского завода технического стекла.",
    ),
    (
        "Стекло формата Over Size", "business", "active",
        "В июле 2022 года на Салаватском заводе выпустили первое листовое стекло формата Over Size "
        "размером 12 000 × 3 210 мм. Летом 2024 года реконструировали линию нанесения покрытий. "
        "В выжимке описано особо прозрачное стекло формата Over Size с мультифункциональным магнетронным покрытием.",
    ),
    (
        "Особо прозрачное стекло Ultraview R", "business", "active",
        "Ultraview R — листовое стекло с пониженным содержанием железа для архитектурных и интерьерных решений. "
        "Оно отличается повышенным светопропусканием и сохраняет нейтральный цвет при увеличенной толщине.",
    ),
    (
        "Антибликовое стекло Clearview", "business", "active",
        "Clearview — стекло с покрытием, которое снижает блики и отражения. Покрытие наносят методом магнетронного "
        "напыления в вакууме. Примеры применения: музейные и торговые витрины, зоопарки, ложи стадионов, "
        "смотровые площадки, автосалоны и фасады.",
    ),
    (
        "Профсоюзная организация предприятия", "faq", "draft",
        "Профсоюзная организация работников АО «Салаватстекло» зарегистрирована как юридическое лицо с 1993 года. "
        "В выжимке указаны избрание профсоюзного комитета в октябре 2014 года и 1228 членов профсоюза. "
        "Актуальный состав комитета и численность требуют подтверждения.",
    ),
    (
        "Фельдшерский здравпункт предприятия", "faq", "draft",
        "В выжимке указан ежедневный режим работы здравпункта с 07:30 до 21:00 без перерыва и выходных. "
        "Актуальное расписание требует подтверждения. Основная задача фельдшеров — экстренная и неотложная помощь "
        "при травмах, внезапных заболеваниях, отравлениях и тяжёлых состояниях.",
    ),
)


def seed_salavat_knowledge(service):
    """Apply the supplied editorial seed to the Salavatsteklo company once."""
    with service.db.transaction() as connection:
        company = connection.execute("SELECT name FROM companies WHERE id=? AND archived_at IS NULL", (COMPANY_ID,)).fetchone()
        if company is None or company["name"] != COMPANY_NAME:
            return []
        connection.execute(
            "CREATE TABLE IF NOT EXISTS knowledge_seed_provenance ("
            "entry_id INTEGER PRIMARY KEY REFERENCES knowledge_entries(id), "
            "company_id INTEGER NOT NULL REFERENCES companies(id), seed_label TEXT NOT NULL, "
            "excerpt_source_id INTEGER NOT NULL REFERENCES sources(id))"
        )
        if connection.execute(
            "SELECT 1 FROM knowledge_seed_provenance WHERE company_id=? AND seed_label=? LIMIT 1",
            (COMPANY_ID, EXCERPT_LABEL),
        ).fetchone():
            return []

        raw_source = _snapshot(service, connection, COMPANY_ID, EXCERPT_LABEL, EXCERPT)
        created = []
        for title, category, status, text in ENTRIES:
            source_id = _snapshot(service, connection, COMPANY_ID, title, text)
            entry_id = connection.execute(
                "INSERT INTO knowledge_entries(company_id,source_id,category,status,origin_url,updated_at) VALUES(?,?,?,?,?,?)",
                (COMPANY_ID, source_id, category, status, EXCERPT_URL, utc_now()),
            ).lastrowid
            connection.execute(
                "INSERT INTO knowledge_seed_provenance(entry_id,company_id,seed_label,excerpt_source_id) VALUES(?,?,?,?)",
                (entry_id, COMPANY_ID, EXCERPT_LABEL, raw_source),
            )
            created.append(entry_id)
        audit_row(connection, COMPANY_ID, "knowledge.seeded", EXCERPT_LABEL)
        return created
