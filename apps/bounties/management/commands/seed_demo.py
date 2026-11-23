import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from apps.bounties.models import Bounty, BountyStatus, Category, Claim, ClaimStatus
from apps.bounties.services import accept_claim

User = get_user_model()


# The demo personas. A user is "seed-owned" only if BOTH username and email match one of these.
SEED_USERS = [
    ("arjun_ece", "arjun.ece@campus.edu.in", "Arjun Sharma", "ECE '26"),
    ("priya_cs", "priya.cs@campus.edu.in", "Priya Nair", "CSE '25"),
    ("rohan_mech", "rohan.mech@campus.edu.in", "Rohan Mehta", "Mech '26"),
    ("ananya_design", "ananya.design@campus.edu.in", "Ananya Sen", "Des '25"),
    ("kabir_ai", "kabir.ai@campus.edu.in", "Kabir Roy", "AI/DS '26"),
    ("meera_biotech", "meera.biotech@campus.edu.in", "Meera Pillai", "Biotech '27"),
    ("campus_mod", "mod@campus.edu.in", "Campus Moderator", "Staff"),
]


class Command(BaseCommand):
    help = (
        "Seeds demo users, campus bounties and claims. DEVELOPMENT ONLY: refuses to run unless "
        "DEBUG is on (or --allow-non-debug is passed). Only touches rows owned by the seed users."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-non-debug",
            action="store_true",
            help="Run even though DEBUG is off. Never use this against a database with real users.",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Password for newly created demo users (default: a random one, printed once).",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["allow_non_debug"]:
            raise CommandError(
                "Refusing to seed: DEBUG is off, so this looks like a real deployment. "
                "seed_demo is for local development. Pass --allow-non-debug only for a throwaway database."
            )

        # Never adopt, modify or delete a real account that merely shares a seed username.
        existing = {u.username: u for u in User.objects.filter(username__in=[u[0] for u in SEED_USERS])}
        for username, email, _full_name, _role in SEED_USERS:
            user = existing.get(username)
            if user and (user.email.lower() != email or user.is_staff or user.is_superuser):
                raise CommandError(
                    f"User {username!r} already exists and is not a seed account "
                    f"(different email, or staff/superuser). Refusing to touch it."
                )

        password = options["password"] or secrets.token_urlsafe(12)

        # Ensure Moderator group exists (created with its permission by accounts migration 0001)
        mod_group, _ = Group.objects.get_or_create(name="Moderators")

        # Create 6 realistic student personas + 1 moderator
        users_data = SEED_USERS

        users = {}
        created_count = 0
        for username, email, full_name, role in users_data:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": email, "first_name": full_name.split()[0], "last_name": full_name.split()[1]},
            )
            if created:  # an existing seed user keeps whatever password it has
                user.set_password(password)
                user.save()
                created_count += 1
            if username == "campus_mod":
                user.groups.add(mod_group)  # moderator by group permission; never is_staff
            users[username] = user

        # Replace only what this command created before: bounties posted by the seed users.
        self.stdout.write(self.style.NOTICE("Clearing previous demo bounties (seed users' only)..."))
        Bounty.objects.filter(poster__in=users.values()).delete()

        self.stdout.write(self.style.SUCCESS(f"Verified {len(users)} student users."))
        if created_count:
            self.stdout.write(f"Password for the {created_count} newly created demo users: {password}")

        now = timezone.now()

        # 25 Realistic Campus Micro-Tasks
        bounties_specs = [
            {
                "poster": "arjun_ece",
                "title": "Debug ESP32 Bluetooth communication failure",
                "category": Category.TECH,
                "reward": 400,
                "deadline": now + timedelta(days=3, hours=4),
                "status": BountyStatus.OPEN,
                "description": (
                    "My ESP32 BLE GATT server drops connections immediately when our Android client app sends "
                    "a notify command during hardware lab experiments. I need someone experienced with ESP-IDF or "
                    "Arduino BLE stack to inspect my callback handlers and fix the buffer disconnect."
                ),
            },
            {
                "poster": "arjun_ece",
                "title": "Solder 16-pin surface mount header on custom PCB",
                "category": Category.TECH,
                "reward": 250,
                "deadline": now + timedelta(days=2),
                "status": BountyStatus.OPEN,
                "description": (
                    "I have a 0.5mm pitch SMD IC and header pins on a mini sensor board. My soldering iron tip is too "
                    "thick and keeps bridging pins. Need an ECE peer with fine-tip soldering gear and flux to attach it cleanly."
                ),
            },
            {
                "poster": "priya_cs",
                "title": "Fix memory leak in PyTorch dataloader for vision lab",
                "category": Category.TECH,
                "reward": 600,
                "deadline": now + timedelta(days=4),
                "status": BountyStatus.OPEN,
                "description": (
                    "During epoch 3 of training a custom ResNet on satellite imagery, RAM usage explodes from 4GB to 24GB "
                    "and crashes our shared lab GPU node. Need someone who understands PyTorch pin_memory, multiprocessing workers, "
                    "and OpenCV tensor conversions."
                ),
            },
            {
                "poster": "priya_cs",
                "title": "Setup Docker Compose for Postgres, Redis and FastAPI",
                "category": Category.TECH,
                "reward": 500,
                "deadline": now + timedelta(days=5),
                "status": BountyStatus.OPEN,
                "description": (
                    "Need a clean docker-compose.yml configuration with named volumes, health checks, and hot-reload mounting "
                    "for our hackathon project backend. Must include automated schema migration script execution on boot."
                ),
            },
            {
                "poster": "ananya_design",
                "title": "Design tech symposium poster in A2 vector format",
                "category": Category.DESIGN,
                "reward": 800,
                "deadline": now + timedelta(days=6),
                "status": BountyStatus.OPEN,
                "description": (
                    "Need an impactful, modern brutalist poster for 'InnoHacks 2026'. Must provide Adobe Illustrator or Figma "
                    "source files with bleed margins ready for high-resolution offset printing at the student union."
                ),
            },
            {
                "poster": "ananya_design",
                "title": "Create high-contrast Figma UI prototype for canteen app",
                "category": Category.DESIGN,
                "reward": 1200,
                "deadline": now + timedelta(days=8),
                "status": BountyStatus.OPEN,
                "description": (
                    "Designing a fast token-booking mobile interface for our university cafeteria. Need 6 core screens: menu feed, "
                    "cart, token status ticker, pickup notification, order history, and feedback. Autolayout required."
                ),
            },
            {
                "poster": "rohan_mech",
                "title": "Convert CAD drawing of planetary gearbox to 3D printable STL",
                "category": Category.DESIGN,
                "reward": 450,
                "deadline": now + timedelta(days=3),
                "status": BountyStatus.OPEN,
                "description": (
                    "I have 2D orthographic blueprints in SolidWorks. Need clean, manifold 3D STL models with 0.2mm tolerance "
                    "offsets for sun, planet gears and ring housing so we can 3D print them on the library Ender-3 printer."
                ),
            },
            {
                "poster": "rohan_mech",
                "title": "Solve finite element mesh distortion in ANSYS beam simulation",
                "category": Category.ACADEMICS,
                "reward": 750,
                "deadline": now + timedelta(days=5),
                "status": BountyStatus.OPEN,
                "description": (
                    "Structural analysis on a stepped cantilever beam keeps failing with negative jacobian mesh elements at the notch fillet. "
                    "Need a senior or fellow mechanical student to guide me through inflation layers or quad meshing parameters."
                ),
            },
            {
                "poster": "kabir_ai",
                "title": "Configure Grafana dashboard on campus Linux server",
                "category": Category.TECH,
                "reward": 650,
                "deadline": now + timedelta(days=7),
                "status": BountyStatus.OPEN,
                "description": (
                    "Our club server runs Prometheus scraping CPU, memory and disk stats from 4 cluster nodes. Need a clean, "
                    "dashboard layout with alert thresholds and temperature gauges for the server room."
                ),
            },
            {
                "poster": "kabir_ai",
                "title": "Set up Git hooks and pre-commit formatting for capstone repo",
                "category": Category.TECH,
                "reward": 300,
                "deadline": now + timedelta(days=4),
                "status": BountyStatus.OPEN,
                "description": (
                    "Configure pre-commit with Ruff, Black, and Flake8 for our 4-person senior project repository so team members "
                    "cannot push unformatted Python code or commit trailing whitespace."
                ),
            },
            {
                "poster": "meera_biotech",
                "title": "Proofread IEEE conference paper draft on LoRaWAN bio-sensors",
                "category": Category.WRITING,
                "reward": 500,
                "deadline": now + timedelta(days=5),
                "status": BountyStatus.OPEN,
                "description": (
                    "Draft is 6 pages long in two-column IEEE LaTeX format. Need thorough technical proofreading for grammar, "
                    "passive-voice clarity, and checking reference formatting against BibTeX standard."
                ),
            },
            {
                "poster": "meera_biotech",
                "title": "Format LaTeX thesis bibliography according to IEEE standards",
                "category": Category.WRITING,
                "reward": 350,
                "deadline": now + timedelta(days=2),
                "status": BountyStatus.OPEN,
                "description": (
                    "34 citations in my literature review have broken DOIs, missing publisher names, and inconsistent conference abbreviations. "
                    "Need a LaTeX pro to clean up my references.bib file."
                ),
            },
            {
                "poster": "arjun_ece",
                "title": "Deliver lab report printouts from North Campus Xerox",
                "category": Category.ERRANDS,
                "reward": 150,
                "deadline": now + timedelta(days=1),
                "status": BountyStatus.OPEN,
                "description": (
                    "I am stuck in analog lab until 5 PM. Need someone around North Campus to pick up my spiral-bound microwave engineering report "
                    "(paid via QR already) and bring it to Ramanujan Block Room 204."
                ),
            },
            {
                "poster": "priya_cs",
                "title": "Simulate 4-bit carry lookahead adder in Verilog",
                "category": Category.ACADEMICS,
                "reward": 400,
                "deadline": now + timedelta(days=3),
                "status": BountyStatus.OPEN,
                "description": (
                    "Need a clean Verilog structural module implementing propagate and generate lookahead equations, along with a "
                    "self-checking testbench that exercises all overflow and carry edge-cases on ModelSim."
                ),
            },
            {
                "poster": "rohan_mech",
                "title": "Pick up Raspberry Pi 4 from Electronics City courier center",
                "category": Category.ERRANDS,
                "reward": 300,
                "deadline": now + timedelta(days=2),
                "status": BountyStatus.OPEN,
                "description": (
                    "Courier delivery failed because our hostel gate was closed. Package is held at the Bluedart hub on Link Road. "
                    "Need a student with a two-wheeler to collect it and drop it at Hostel 7 security."
                ),
            },
            {
                "poster": "ananya_design",
                "title": "Vectorize hand-drawn robotics club crest for team jerseys",
                "category": Category.DESIGN,
                "reward": 450,
                "deadline": now + timedelta(days=6),
                "status": BountyStatus.OPEN,
                "description": (
                    "We have a pencil sketch of our club mascot gear and lightning bolt. Need it traced into clean SVG paths with "
                    "single-color and two-color variants suitable for screen-printing on cotton hoodies."
                ),
            },
            {
                "poster": "kabir_ai",
                "title": "Review and clean up Jupyter Notebook code for project submission",
                "category": Category.ACADEMICS,
                "reward": 350,
                "deadline": now + timedelta(days=4),
                "status": BountyStatus.OPEN,
                "description": (
                    "Notebook is full of out-of-order cells and leftover print statements from debugging. Need someone to modularize it "
                    "into clear functions, add markdown headings, and verify it runs cleanly from Kernel Restart & Run All."
                ),
            },
            {
                "poster": "meera_biotech",
                "title": "Transcribe recorded interview with faculty adviser on biosensors",
                "category": Category.WRITING,
                "reward": 400,
                "deadline": now + timedelta(days=3),
                "status": BountyStatus.OPEN,
                "description": (
                    "35-minute audio recording in English discussing microfluidic sensor challenges. Need accurate verbatim transcription "
                    "with timestamps every 3 minutes in a clean Markdown document."
                ),
            },
            {
                "poster": "arjun_ece",
                "title": "Buy Arduino Mega and stepper driver shield from SP Road",
                "category": Category.ERRANDS,
                "reward": 350,
                "deadline": now + timedelta(days=2),
                "status": BountyStatus.OPEN,
                "description": (
                    "Visiting SP Road or electronics market today? Need someone to buy 1x Arduino Mega 2560 and 2x A4988 stepper drivers. "
                    "I will reimburse the component bill immediately on top of the ₹350 bounty."
                ),
            },
            {
                "poster": "priya_cs",
                "title": "Debug ROS2 node publisher dropped packets over local WiFi",
                "category": Category.TECH,
                "reward": 850,
                "deadline": now + timedelta(days=5),
                "status": BountyStatus.OPEN,
                "description": (
                    "Our autonomous rover camera publisher drops ~40% of frames over campus WiFi. Need someone with CycloneDDS / FastDDS "
                    "QoS configuration expertise to tune reliability, history depth, and deadline policies."
                ),
            },
            # Bounties with CLAIMED / DONE status to showcase complete marketplace history
            {
                "poster": "rohan_mech",
                "title": "Derive state space representation for inverted pendulum",
                "category": Category.ACADEMICS,
                "reward": 500,
                "deadline": now + timedelta(days=1),
                "status": BountyStatus.OPEN,
                "description": (
                    "Need step-by-step mathematical derivation of linear state-space matrices (A, B, C, D) around upright equilibrium, "
                    "including verification of controllability and observability in MATLAB."
                ),
            },
            {
                "poster": "ananya_design",
                "title": "Redesign IEEE student branch newsletter cover",
                "category": Category.DESIGN,
                "reward": 350,
                "deadline": now + timedelta(days=2),
                "status": BountyStatus.OPEN,
                "description": (
                    "Redesign quarterly edition cover celebrating our robotics team's national win. Clean typography and photo collage layout."
                ),
            },
            {
                "poster": "kabir_ai",
                "title": "Fix GCC linking error with LAPACK library on Ubuntu",
                "category": Category.TECH,
                "reward": 300,
                "deadline": now - timedelta(days=4),
                "status": BountyStatus.DONE,
                "description": (
                    "Undefined reference to `dgesv_` when building scientific computing lab 2 code. Needed help with linker flags and library path."
                ),
            },
            {
                "poster": "meera_biotech",
                "title": "Translate survey responses from Hindi to English",
                "category": Category.WRITING,
                "reward": 300,
                "deadline": now - timedelta(days=1),
                "status": BountyStatus.DONE,
                "description": (
                    "85 brief qualitative survey responses on campus dining habits. Needed accurate English translations for sociology study."
                ),
            },
            {
                "poster": "arjun_ece",
                "title": "Troubleshoot thermal camera sensor I2C address conflict",
                "category": Category.TECH,
                "reward": 350,
                "deadline": now + timedelta(days=3),
                "status": BountyStatus.CANCELLED,
                "description": (
                    "MLX90640 thermal sensor wasn't appearing on I2C bus scan. Found a loose pull-up resistor wire myself so closing this bounty."
                ),
            },
        ]

        created_bounties = []
        for spec in bounties_specs:
            poster_user = users[spec["poster"]]
            bounty = Bounty(
                poster=poster_user,
                title=spec["title"],
                category=spec["category"],
                reward=spec["reward"],
                deadline=spec["deadline"],
                status=spec["status"],
                description=spec["description"],
            )
            # Bypass validation for expired seed data
            bounty.save()
            created_bounties.append(bounty)

        self.stdout.write(self.style.SUCCESS(f"Created {len(created_bounties)} realistic campus bounties."))

        # Seed Claims on a few bounties
        # 1. On "Debug ESP32 Bluetooth communication failure" by arjun_ece -> claims from priya_cs and kabir_ai
        b1 = created_bounties[0]
        c1 = Claim.objects.create(
            bounty=b1,
            claimant=users["priya_cs"],
            message="I built a custom BLE telemetry bridge last semester using ESP-IDF. I can fix this in under an hour tonight.",
            contact="WhatsApp: +91 98450 11223",
            status=ClaimStatus.PENDING,
        )
        c2 = Claim.objects.create(
            bounty=b1,
            claimant=users["kabir_ai"],
            message="I have an nRF Sniffer dongle with me in Hostel 4. We can capture Wireshark BLE packets together and diagnose.",
            contact="WhatsApp: +91 97410 44556",
            status=ClaimStatus.PENDING,
        )

        # 2. On "Derive state space representation" by rohan_mech -> claimed by arjun_ece and accepted!
        b_claimed = created_bounties[20]
        c_claimed = Claim.objects.create(
            bounty=b_claimed,
            claimant=users["arjun_ece"],
            message="Control systems is my core elective. I have the complete Lagrangian derivation in my notebook already.",
            contact="WhatsApp: +91 91234 56789",
            status=ClaimStatus.PENDING,
        )
        accept_claim(c_claimed.id, users["rohan_mech"])

        # 3. On "Redesign IEEE newsletter cover" -> done bounty
        b_done = created_bounties[21]
        c_done = Claim.objects.create(
            bounty=b_done,
            claimant=users["priya_cs"],
            message="Delivered Figma vector exports and high-res PDF. Work completed.",
            contact="priya.cs@campus.edu.in",
            status=ClaimStatus.PENDING,
        )
        accept_claim(c_done.id, users["ananya_design"])
        b_done.refresh_from_db()
        b_done.status = BountyStatus.DONE
        b_done.save(update_fields=["status"])

        self.stdout.write(self.style.SUCCESS("Demo database successfully seeded with 6 students, 1 moderator, 25 bounties, and realistic claims!"))
