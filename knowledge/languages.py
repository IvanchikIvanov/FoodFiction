"""Supported answer languages and bounded service messages (not company facts)."""

from typing import Literal

AnswerLanguage = Literal["ru-RU", "uz-UZ", "tg-TJ", "ky-KG", "kk-KZ", "en-US", "hy-AM", "az-AZ", "tr-TR", "zh-CN"]

# Translation terminology only; it does not assert that a recipe uses chicken.
BAKED_CHICKEN = {
    "ru-RU": "запечённая курица",
    "uz-UZ": "duxovkada pishirilgan tovuq go‘shti",
    "tg-TJ": "гӯшти мурғи дар танӯр пухташуда",
    "ky-KG": "меште бышырылган тоок эти",
    "kk-KZ": "пеште пісірілген тауық еті",
    "en-US": "oven-baked chicken",
    "hy-AM": "ջեռոցում թխված հավի միս",
    "az-AZ": "sobada bişirilmiş toyuq əti",
    "tr-TR": "fırında pişirilmiş tavuk",
    "zh-CN": "烤箱烤制的鸡肉",
}

LANGUAGES = {
    "ru-RU": ("русский", "В доступных материалах не нашёл: {missing}.",
              "В подключённой базе знаний нет сведений для ответа на этот вопрос.",
              "Не удалось сформировать ответ. Попробуйте позже или откройте источник.",
              "сведения для ответа на этот вопрос"),
    "uz-UZ": ("o‘zbek tili", "Mavjud materiallardan quyidagi ma’lumotni topmadim: {missing}.",
              "Ulangan bilimlar bazasida bu savolga javob berish uchun ma’lumot yo‘q.",
              "Javobni tayyorlab bo‘lmadi. Keyinroq urinib ko‘ring yoki manbani oching.",
              "bu savolga javob berish uchun ma’lumot"),
    "tg-TJ": ("тоҷикӣ", "Дар маводи дастрас маълумоти зеринро наёфтам: {missing}.",
              "Дар пойгоҳи дониши пайвастшуда маълумот барои ҷавоб ба ин савол нест.",
              "Ҷавобро омода карда натавонистам. Баъдтар кӯшиш кунед ё манбаъро кушоед.",
              "маълумот барои ҷавоб ба ин савол"),
    "ky-KG": ("кыргызча", "Жеткиликтүү материалдардан төмөнкү маалыматты тапкан жокмун: {missing}.",
              "Туташтырылган билим базасында бул суроого жооп берүү үчүн маалымат жок.",
              "Жооп даярдалган жок. Кийинчерээк аракет кылыңыз же булакты ачыңыз.",
              "бул суроого жооп берүү үчүн маалымат"),
    "kk-KZ": ("қазақ тілі", "Қолжетімді материалдардан мына мәліметті таппадым: {missing}.",
              "Қосылған білім базасында бұл сұраққа жауап беретін мәлімет жоқ.",
              "Жауап дайындау мүмкін болмады. Кейінірек қайталаңыз немесе дереккөзді ашыңыз.",
              "бұл сұраққа жауап беретін мәлімет"),
    "en-US": ("English", "I could not find this information in the available materials: {missing}.",
              "The connected knowledge base does not contain information to answer this question.",
              "I could not generate an answer. Please try again later or open the source.",
              "information to answer this question"),
    "hy-AM": ("հայերեն", "Հասանելի նյութերում չգտա հետևյալ տեղեկությունը՝ {missing}։",
              "Միացված գիտելիքների բազայում այս հարցին պատասխանելու համար տեղեկություն չկա։",
              "Չհաջողվեց պատրաստել պատասխանը։ Փորձեք ավելի ուշ կամ բացեք աղբյուրը։",
              "այս հարցին պատասխանելու համար տեղեկություն"),
    "az-AZ": ("Azərbaycan dili", "Mövcud materiallarda bu məlumatı tapa bilmədim: {missing}.",
              "Qoşulmuş bilik bazasında bu suala cavab vermək üçün məlumat yoxdur.",
              "Cavab hazırlamaq mümkün olmadı. Daha sonra yenidən cəhd edin və ya mənbəni açın.",
              "bu suala cavab vermək üçün məlumat"),
    "tr-TR": ("Türkçe", "Mevcut materyallerde şu bilgiyi bulamadım: {missing}.",
              "Bağlı bilgi tabanında bu soruyu yanıtlayacak bilgi bulunmuyor.",
              "Yanıt oluşturulamadı. Daha sonra tekrar deneyin veya kaynağı açın.",
              "bu soruyu yanıtlayacak bilgi"),
    "zh-CN": ("简体中文", "在现有资料中未找到以下信息：{missing}。",
              "已连接的知识库中没有可用于回答此问题的信息。",
              "暂时无法生成回答，请稍后重试或打开来源文档。",
              "可用于回答此问题的信息"),
}
