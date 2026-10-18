"""Management command to seed video presets and billing packages."""
from decimal import Decimal
from django.core.management.base import BaseCommand
from apps.billing.models import GenerationPackage
from apps.videos.models import VideoPreset


class Command(BaseCommand):
    help = "Заполняет начальные тарифы VideoLab AI и шаблоны задач редактирования"

    def handle(self, *args, **options):
        # 1. Стартовые тарифы VideoLab AI (Single 299 ₽, Trio 849 ₽, Starter 2 190 ₽, Pro 5 750 ₽)
        pkg1, _ = GenerationPackage.objects.update_or_create(
            slug="video_single",
            defaults=dict(
                title="Single (1 кредит)",
                price=Decimal("299.00"),
                currency="RUB",
                generations=1,
                is_active=True,
                is_featured=False,
                badge="",
                features="1 кредит на видео\nДля знакомства с продуктом\nМодель Gemini Omni",
                sort_order=1,
            ),
        )

        pkg3, _ = GenerationPackage.objects.update_or_create(
            slug="video_pack_3",
            defaults=dict(
                title="Trio (3 кредита)",
                price=Decimal("849.00"),
                currency="RUB",
                generations=3,
                is_active=True,
                is_featured=False,
                badge="",
                features="3 кредита на видео\nДля первых экспериментов\nЭкономия 15%",
                sort_order=2,
            ),
        )

        pkg10, _ = GenerationPackage.objects.update_or_create(
            slug="video_starter",
            defaults=dict(
                title="Starter (10 кредитов)",
                price=Decimal("2190.00"),
                currency="RUB",
                generations=10,
                is_active=True,
                is_featured=True,
                badge="Основной тариф",
                features="10 кредитов на видео\nДля SMM и регулярного контента\n219 ₽ за кредит (скидка 27%)",
                sort_order=3,
            ),
        )

        pkg30, _ = GenerationPackage.objects.update_or_create(
            slug="video_pro",
            defaults=dict(
                title="Pro (30 кредитов)",
                price=Decimal("5750.00"),
                currency="RUB",
                generations=30,
                is_active=True,
                is_featured=False,
                badge="Максимум",
                features="30 кредитов на видео\nДля активных видеомейкеров и агентств\n192 ₽ за кредит (скидка 36%)",
                sort_order=4,
            ),
        )

        # Деактивируем неактуальные тарифы
        active_slugs = ["video_single", "video_pack_3", "video_starter", "video_pro"]
        GenerationPackage.objects.exclude(slug__in=active_slugs).update(is_active=False)

        self.stdout.write(
            self.style.SUCCESS(
                f"Тарифы сохранены: Single (299 руб.), Trio (849 руб.), Starter (2 190 руб.), Pro (5 750 руб.)"
            )
        )

        # 2. Шаблоны редактирования (ТЗ раздел 3.4)
        presets = [
            {
                "category": VideoPreset.Category.LIGHTING,
                "title": "Закатный золотой час",
                "slug": "golden-hour-lighting",
                "description": "Мягкий золотистый свет заката, кинематографичные тени и теплые тона",
                "prompt_template": (
                    "Transform the atmosphere of the video to a cinematic golden hour sunset: "
                    "add soft warm golden lighting, rich amber glow, natural lens flares, dramatic cinematic shadows. "
                    "Preserve all original character movements, actions, camera motion and key subject identity unchanged."
                ),
                "sort_order": 1,
            },
            {
                "category": VideoPreset.Category.LIGHTING,
                "title": "Ночной неоновый свет",
                "slug": "cyberpunk-night-light",
                "description": "Киберпанк-освещение, неоновые синие и пурпурные рефлексы",
                "prompt_template": (
                    "Alter the video scene into a moody cyberpunk night scene: deep contrast, vibrant blue and magenta neon "
                    "reflections, dark ambient atmosphere, keeping character silhouette and camera dynamics identical."
                ),
                "sort_order": 2,
            },
            {
                "category": VideoPreset.Category.EFFECT,
                "title": "Световые блики и дымка",
                "slug": "light-leaks-mist",
                "description": "Оптические световые блики, анаморфное сияние и легкий кинематографичный туман",
                "prompt_template": (
                    "Add subtle optical light leaks, anamorphic bokeh glow and light atmospheric fog in the background. "
                    "Do not alter the subject face, body, or core action; only enhance the visual atmosphere."
                ),
                "sort_order": 3,
            },
            {
                "category": VideoPreset.Category.EFFECT,
                "title": "Магические частицы и сияние",
                "slug": "magic-particles",
                "description": "Парящие искры, золотые микрочастицы и свечение вокруг ключевых объектов",
                "prompt_template": (
                    "Introduce soft floating golden ember particles and subtle radiant glow around movement contours, "
                    "giving a magical advertising feel, maintaining clean product and subject appearance."
                ),
                "sort_order": 4,
            },
            {
                "category": VideoPreset.Category.STYLE,
                "title": "Винтаж 35мм плёнка",
                "slug": "vintage-35mm-film",
                "description": "Текстура ретро-кинопленки Kodak, естественное зерно и ностальгическая цветокоррекция",
                "prompt_template": (
                    "Apply authentic 35mm Kodak motion picture film look: organic fine grain, film halation, vintage warm color palette, "
                    "analog aesthetic, perfectly retaining the original video composition and timing."
                ),
                "sort_order": 5,
            },
            {
                "category": VideoPreset.Category.STYLE,
                "title": "Коммерческий глянец (Commercial HD)",
                "slug": "commercial-clean-look",
                "description": "Чистая рекламная картинка, идеальная четкость и сочные коммерческие цвета",
                "prompt_template": (
                    "Enhance video to a high-end commercial advertising look: studio sharpness, pristine color fidelity, "
                    "clean highlights, balanced studio contrast, keeping character and brand elements perfectly preserved."
                ),
                "sort_order": 6,
            },
        ]

        for p_data in presets:
            preset_obj, created = VideoPreset.objects.update_or_create(
                slug=p_data["slug"],
                defaults=p_data,
            )
            status_text = "создан" if created else "обновлен"
            self.stdout.write(f"Пресет '{preset_obj.title}' {status_text}")

        self.stdout.write(self.style.SUCCESS("Заполнение начальных данных VideoLab AI успешно завершено!"))
