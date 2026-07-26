"""Seed a local development database with a small, realistic dataset.

Creates 40 users, 16 problems and 2 concurrent 5-hour contests (10 problems
each, overlapping), plus submissions spread through the contests, in the final
hour, and outside of contest time.

The two contests run at the same time as two divisions over a shared problem
set, so entrants are partitioned between them. Problems are all-or-nothing, and
are ordered by difficulty: each user has a skill level, solves the easy end of
the set and tails off, so solve counts decrease along the problem order and the
scoreboard reads as a staircase.

Everything created here is prefixed (users: `user01`, problems: `devp01`,
contests: `devcon1`) so `--wipe` can remove it again without touching other
data. Judging is never invoked -- submissions are written pre-graded.

Usage:
    ./manage.py seed_dev_data --wipe
"""

import random
from datetime import timedelta
from math import exp

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from judge.models import (
    Contest,
    ContestParticipation,
    ContestProblem,
    ContestSubmission,
    Language,
    Organization,
    Problem,
    ProblemGroup,
    ProblemType,
    Profile,
    Submission,
    SubmissionSource,
    SubmissionTestCase,
)

USER_PREFIX = "user"
PROBLEM_PREFIX = "devp"
CONTEST_KEYS = ("devcon1", "devcon2")
DEFAULT_PASSWORD = "password"

# Organisations the scoreboard reads: one says who is sitting in the hall, the
# rest are eligibility categories shown as badges. Membership overlaps, and the
# share is the fraction of the field that joins each.
#
# Keep the slugs in step with MCPC_SCOREBOARDS in local_settings.py.
ORGANIZATIONS = [
    {
        "slug": "dev-onsite",
        "name": "Development On-site",
        "short_name": "On-site",
        "about": "Seeded development organisation: competitors sitting in the hall.",
        "share": 0.55,
    },
    {
        "slug": "dev-beginner",
        "name": "Development Beginner",
        "short_name": "Beginner",
        "about": "Seeded development organisation: beginner division eligibility.",
        "share": 0.40,
    },
]

FIRST_NAMES = [
    "Ada",
    "Alan",
    "Grace",
    "Linus",
    "Barbara",
    "Ken",
    "Margaret",
    "Dennis",
    "Edsger",
    "Donald",
    "Radia",
    "Vint",
    "Frances",
    "Tim",
    "Katherine",
    "John",
    "Anita",
    "Guido",
    "Jean",
    "Bjarne",
]
LAST_NAMES = [
    "Lovelace",
    "Turing",
    "Hopper",
    "Torvalds",
    "Liskov",
    "Thompson",
    "Hamilton",
    "Ritchie",
    "Dijkstra",
    "Knuth",
    "Perlman",
    "Cerf",
    "Allen",
    "Berners-Lee",
    "Johnson",
    "McCarthy",
    "Borg",
    "van Rossum",
    "Sammet",
    "Stroustrup",
]

# Ordered easiest to hardest -- problem N is the Nth hardest of the whole set.
PROBLEM_NAMES = [
    "Coin Rows",
    "Palindrome Factory",
    "Grid Escape",
    "Bracket Repair",
    "Train Scheduling",
    "Median Maintenance",
    "Sparse Forest",
    "Lexicographic Walk",
    "Bitmask Buffet",
    "Prefix Sums Redux",
    "Meeting Point",
    "Chromatic Fences",
    "Modular Staircase",
    "Convex Delivery",
    "Rolling Hash Hunt",
    "Persistent Queries",
]

PROBLEM_BODY = (
    "This is seeded development data, not a real problem statement.\n\n"
    "Given an integer $n$ and a sequence $a_1, \\ldots, a_n$, compute the answer.\n\n"
    "## Input\n\nThe first line contains $n$ ($1 \\le n \\le 10^5$).\n\n"
    "## Output\n\nA single integer.\n"
)

# Verdicts for a failed attempt, and their relative weights. Problems are
# all-or-nothing, so there is no partial-score verdict.
FAILURE_WEIGHTS = [
    ("WA", 45),
    ("TLE", 20),
    ("RTE", 12),
    ("MLE", 8),
    ("IR", 5),
    ("CE", 10),
]

# The tail of the contest that a frozen scoreboard hides. Kept in step with
# MCPC_SCOREBOARD_FREEZE_MINUTES (60 minutes of a 5 hour contest).
FREEZE_FRACTION = 0.2

# Peak chance that a run's deciding attempt is pushed into the frozen window,
# before the marginality weighting in late_finish_probability. This is what
# makes the reveal worth watching: without it the only submissions in the final
# hour are doomed attempts at the hardest problems, so unfreezing the board
# turns everything red and nobody moves.
#
# Tuned so that roughly a fifth of all submissions land in the final hour --
# about what an even spread over a five hour contest would give -- and each
# division reveals a dozen or so solves. Raising it much past 0.35 stops
# looking like a contest and starts looking like a shuffle.
LATE_FINISH_CHANCE = 0.25

SOURCE_SNIPPETS = {
    "PY3": "import sys\n\n\ndef main():\n    data = sys.stdin.read().split()\n    print(len(data))\n\n\nmain()\n",
    "CPP17": (
        "#include <bits/stdc++.h>\nusing namespace std;\n\n"
        "int main() {\n    int n;\n    cin >> n;\n    cout << n << endl;\n}\n"
    ),
    "JAVA": (
        "public class Main {\n    public static void main(String[] args) {\n"
        '        System.out.println("seed");\n    }\n}\n'
    ),
}
DEFAULT_SOURCE = "// seeded development submission\n"


class Command(BaseCommand):
    help = "seeds the database with development fixtures (users, problems, contests, submissions)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--users",
            type=int,
            default=40,
            help="number of users to create (default: 40)",
        )
        parser.add_argument(
            "--problems",
            type=int,
            default=16,
            help="number of problems to create (default: 16)",
        )
        parser.add_argument(
            "--seed", type=int, default=20260726, help="RNG seed, for reproducible data"
        )
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help="password given to every seeded user",
        )
        parser.add_argument(
            "--wipe", action="store_true", help="delete previously seeded objects first"
        )
        parser.add_argument(
            "--no-admin",
            action="store_true",
            help="do not create the `admin` superuser",
        )
        parser.add_argument(
            "--no-rate", action="store_true", help="skip contest rating calculation"
        )

    @transaction.atomic
    def handle(self, *args, **options):
        self.rng = random.Random(options["seed"])
        self.verbosity = options["verbosity"]

        if options["problems"] < 10:
            raise CommandError("need at least 10 problems to fill a contest")

        languages = list(Language.objects.all())
        if not languages:
            raise CommandError(
                "no languages in the database; run `manage.py loaddata language_small` first"
            )
        self.languages = languages

        if options["wipe"]:
            self.wipe()

        admin = None if options["no_admin"] else self.create_admin(options["password"])
        profiles = self.create_users(options["users"], options["password"])
        self.create_organizations(profiles)
        problems = self.create_problems(options["problems"], admin or profiles[0])
        contests = self.create_contests(problems, admin or profiles[0])

        submissions = []
        for contest, entrants in self.assign_entrants(contests, profiles):
            submissions += self.create_contest_activity(contest, entrants)
        submissions += self.create_practice_submissions(problems, profiles)

        self.apply_dates(submissions)
        self.recompute(contests, problems, profiles, rate=not options["no_rate"])

        self.stdout.write(
            self.style.SUCCESS(
                "Seeded %d users, %d problems, %d contests, %d submissions."
                % (len(profiles), len(problems), len(contests), len(submissions)),
            )
        )
        self.stdout.write(
            "Log in as any of %s01..%s%02d (password: %s)%s"
            % (
                USER_PREFIX,
                USER_PREFIX,
                len(profiles),
                options["password"],
                (
                    ""
                    if admin is None
                    else ", or admin/%s for the admin site" % options["password"]
                ),
            )
        )

    # ------------------------------------------------------------------ wipe

    def wipe(self):
        Contest.objects.filter(key__in=CONTEST_KEYS).delete()
        Problem.objects.filter(code__startswith=PROBLEM_PREFIX).delete()
        User.objects.filter(username__startswith=USER_PREFIX).delete()
        ProblemGroup.objects.filter(name="dev").delete()
        Organization.objects.filter(
            slug__in=[org["slug"] for org in ORGANIZATIONS]
        ).delete()
        if self.verbosity:
            self.stdout.write("Wiped previously seeded objects.")

    # ------------------------------------------------------------------ users

    def create_admin(self, password):
        user, created = User.objects.get_or_create(
            username="admin",
            defaults={
                "email": "admin@example.com",
                "is_staff": True,
                "is_superuser": True,
            },
        )
        if created:
            user.set_password(password)
            user.save()
        profile, _created = Profile.objects.get_or_create(
            user=user,
            defaults={"language": self.languages[0], "display_rank": "admin"},
        )
        return profile

    def create_users(self, count, password):
        profiles = []
        for i in range(1, count + 1):
            username = "%s%02d" % (USER_PREFIX, i)
            first = FIRST_NAMES[(i - 1) % len(FIRST_NAMES)]
            last = LAST_NAMES[(i * 7 - 1) % len(LAST_NAMES)]
            user = User(
                username=username,
                email="%s@example.com" % username,
                first_name=first,
                last_name=last,
                is_active=True,
            )
            user.set_password(password)
            user.save()
            profiles.append(
                Profile.objects.create(
                    user=user,
                    language=self.rng.choice(self.languages),
                    about="Seeded development user %s." % username,
                    display_rank="setter" if i <= 3 else "user",
                    timezone="Australia/Melbourne",
                    last_access=timezone.now()
                    - timedelta(days=self.rng.randint(0, 20)),
                    ip="127.0.0.1",
                )
            )
        if self.verbosity:
            self.stdout.write("Created %d users." % len(profiles))
        return profiles

    # --------------------------------------------------------------- problems

    def create_organizations(self, profiles):
        """Create the badge organisations and sprinkle members through them.

        Membership deliberately overlaps -- a first year sitting in the hall is
        in two of them -- so the scoreboard renders more than one badge per row
        and the in-person toggle cuts across the other categories.
        """
        organizations = []
        for spec in ORGANIZATIONS:
            organization, _created = Organization.objects.get_or_create(
                slug=spec["slug"],
                defaults={
                    "name": spec["name"],
                    "short_name": spec["short_name"],
                    "about": spec["about"],
                    "is_open": True,
                },
            )
            members = [p for p in profiles if self.rng.random() < spec["share"]]
            organization.members.set(members)
            organizations.append((organization, len(members)))

        if self.verbosity:
            self.stdout.write(
                "Created %d organizations: %s."
                % (
                    len(organizations),
                    ", ".join(
                        "%s (%d members)" % (org.slug, count)
                        for org, count in organizations
                    ),
                )
            )
        return [org for org, _count in organizations]

    def create_problems(self, count, author):
        group, _created = ProblemGroup.objects.get_or_create(
            name="dev", defaults={"full_name": "Development"}
        )
        types = []
        for name, full_name in (
            ("dp", "Dynamic Programming"),
            ("graph", "Graph Theory"),
            ("adhoc", "Ad Hoc"),
        ):
            ptype, _created = ProblemType.objects.get_or_create(
                name=name, defaults={"full_name": full_name}
            )
            types.append(ptype)

        published = timezone.now() - timedelta(days=40)
        problems = []
        for i in range(1, count + 1):
            name = PROBLEM_NAMES[(i - 1) % len(PROBLEM_NAMES)]
            problem = Problem.objects.create(
                code="%s%02d" % (PROBLEM_PREFIX, i),
                name=name,
                description=PROBLEM_BODY,
                group=group,
                time_limit=self.rng.choice([1.0, 2.0, 3.0]),
                memory_limit=self.rng.choice([65536, 131072, 262144]),
                # Point value rises with difficulty: devp01 is the easiest.
                points=float(3 + 2 * (i - 1)),
                partial=False,  # all or nothing
                is_public=True,
                is_manually_managed=True,  # never queued for judging
                date=published + timedelta(days=i),
                summary="Seeded development problem %d." % i,
            )
            problem.authors.add(author)
            problem.types.set(self.rng.sample(types, self.rng.randint(1, 2)))
            problem.allowed_languages.set(self.languages)
            problems.append(problem)
        if self.verbosity:
            self.stdout.write("Created %d problems." % len(problems))
        return problems

    # --------------------------------------------------------------- contests

    def create_contests(self, problems, author):
        # Both divisions run simultaneously over a shared, overlapping problem set.
        start = (timezone.now() - timedelta(days=14)).replace(
            minute=0, second=0, microsecond=0
        )
        specs = [
            {
                "key": CONTEST_KEYS[0],
                "name": "MCPC Dev Contest Div B",
                "format_name": "default",
                "format_config": None,
                "problems": problems[0:10],
            },
            {
                "key": CONTEST_KEYS[1],
                "name": "MCPC Dev Contest Div A",
                "format_name": "icpc",
                "format_config": {"penalty": 20},
                # Shares its four easiest problems with the Novice division.
                "problems": problems[6:16],
            },
        ]

        contests = []
        for spec in specs:
            contest = Contest.objects.create(
                key=spec["key"],
                name=spec["name"],
                description="Seeded development contest. Five hours, already finished.",
                start_time=start,
                end_time=start + timedelta(hours=5),
                time_limit=None,
                is_visible=True,
                is_rated=True,
                rate_all=False,
                use_clarifications=True,
                scoreboard_visibility=Contest.SCOREBOARD_VISIBLE,
                format_name=spec["format_name"],
                format_config=spec["format_config"],
                summary="Seeded development contest.",
                points_precision=2,
            )
            contest.authors.add(author)
            # Problems are listed easiest first, so `order` is difficulty order.
            for order, problem in enumerate(spec["problems"], start=1):
                ContestProblem.objects.create(
                    contest=contest,
                    problem=problem,
                    points=int(problem.points),
                    partial=False,
                    order=order,
                )
            contests.append(contest)
        if self.verbosity:
            self.stdout.write(
                "Created %d contests, running %s to %s."
                % (len(contests), start, start + timedelta(hours=5))
            )
        return contests

    # ------------------------------------------------------------ submissions

    def assign_entrants(self, contests, profiles):
        """Split users between the concurrent divisions; nobody is in both.

        The Open division gets the stronger half of the field, which keeps both
        scoreboards looking like a staircase rather than one being all zeroes.
        """
        shuffled = list(profiles)
        self.rng.shuffle(shuffled)
        novice_size = int(len(shuffled) * 0.55)
        entered_size = int(len(shuffled) * 0.95)  # the rest sat this one out

        novice = [(p, self.rng.betavariate(2, 4)) for p in shuffled[:novice_size]]
        open_div = [
            (p, self.rng.betavariate(3, 2.5))
            for p in shuffled[novice_size:entered_size]
        ]
        return list(zip(contests, (novice, open_div)))

    def solve_probability(self, skill, index, count):
        """Chance a user of the given skill solves the index-th hardest problem."""
        position = (index + 0.5) / count
        return min(0.97, max(0.01, 1.0 / (1.0 + exp((position - skill) * 9.0))))

    def create_contest_activity(self, contest, entrants):
        """Create participations and in-contest submissions for one contest."""
        contest_problems = list(
            contest.contest_problems.select_related("problem").all()
        )
        duration = contest.end_time - contest.start_time

        submissions = []
        for profile, skill in entrants:
            participation = ContestParticipation.objects.create(
                contest=contest,
                user=profile,
                real_start=contest.start_time,
                virtual=ContestParticipation.LIVE,
            )
            submissions += self.create_attempts(
                participation,
                contest,
                contest_problems,
                skill,
                duration,
                contest.start_time,
            )

        # A couple of virtual participations after the contest ended.
        for profile in self.rng.sample([p for p, _skill in entrants], 3):
            real_start = contest.end_time + timedelta(days=self.rng.randint(1, 3))
            participation = ContestParticipation.objects.create(
                contest=contest,
                user=profile,
                real_start=real_start,
                virtual=1,
            )
            submissions += self.create_attempts(
                participation,
                contest,
                contest_problems,
                self.rng.betavariate(3, 3),
                duration,
                real_start,
            )
        return submissions

    def create_attempts(
        self, participation, contest, contest_problems, skill, duration, start
    ):
        """One user's run at one contest: solves the easy end, tails off, gives up."""
        submissions = []
        count = len(contest_problems)
        for index, contest_problem in enumerate(contest_problems):
            chance = self.solve_probability(skill, index, count)
            # Users also submit to a problem or two past what they can solve.
            if self.rng.random() > min(1.0, chance + 0.3):
                continue
            solved = self.rng.random() < chance
            late = self.rng.random() < self.late_finish_probability(chance)
            for offset, result in self.attempt_timeline(
                index, count, duration, chance, solved, late
            ):
                submissions.append(
                    self.build_submission(
                        profile=participation.user,
                        problem=contest_problem.problem,
                        when=start + offset,
                        result=result,
                        contest=contest,
                        contest_problem=contest_problem,
                        participation=participation,
                    )
                )
        return submissions

    def late_finish_probability(self, chance):
        """How likely this run's deciding attempt lands inside the freeze.

        Weighted towards problems the user is marginal on. A team does not sit
        on a problem it finds easy until the last hour, and a problem far
        beyond it produces a red cell that moves nobody; the interesting cells
        are the ones the team might just get, so the weight peaks at chance 0.5
        and falls to zero at either extreme.
        """
        return LATE_FINISH_CHANCE * 4.0 * chance * (1.0 - chance)

    def attempt_timeline(self, index, count, duration, chance, solved, late=False):
        """Yield (offset into the contest, verdict) for one user on one problem.

        Harder problems are attempted later and take more tries, so easy solves
        cluster early and the final hour fills up with the hard end of the set.

        When ``late`` is set the deciding attempt is moved into the frozen
        window instead, and the earlier tries are spread further apart so some
        of them still land in the open. That produces the three cases a reveal
        needs to be interesting: a frozen cell that turns green and moves the
        team up, one that turns red and leaves them where they were, and one
        that already had visible failures before the freeze.
        """
        seconds = duration.total_seconds()
        freeze_start = seconds * (1.0 - FREEZE_FRACTION)
        tries = 1 + int(self.rng.random() * (1 + 2 * (1 - chance)))

        if late:
            last = self.rng.uniform(freeze_start + 60, seconds - 60)
            # Wider spacing so earlier attempts can predate the freeze.
            gap = seconds * self.rng.uniform(0.04, 0.11)
        else:
            slowness = 1.15 - 0.45 * chance
            position = 0.18 + 0.82 * (index + 1) / count
            fraction = min(0.995, position * self.rng.uniform(0.55, 1.15) * slowness)
            last = seconds * fraction
            gap = seconds * 0.03

        attempts = []
        for step in range(tries - 1, -1, -1):
            when = last - gap * step * self.rng.uniform(0.5, 1.5)
            if when < 0:
                continue
            final = step == 0
            attempts.append(
                (
                    timedelta(seconds=when),
                    "AC" if (final and solved) else self.pick_failure(),
                )
            )
        return attempts

    def pick_failure(self):
        total = sum(weight for _result, weight in FAILURE_WEIGHTS)
        pick = self.rng.uniform(0, total)
        upto = 0
        for result, weight in FAILURE_WEIGHTS:
            upto += weight
            if pick <= upto:
                return result
        return "WA"

    def create_practice_submissions(self, problems, profiles):
        """Submissions unattached to any contest: before, between and after the contests."""
        now = timezone.now()
        submissions = []
        for _ in range(120):
            profile = self.rng.choice(profiles)
            index = self.rng.randrange(len(problems))
            skill = self.rng.betavariate(3, 3)
            solved = self.rng.random() < self.solve_probability(
                skill, index, len(problems)
            )
            # Stay inside the window where every problem has already been published.
            when = now - timedelta(
                days=self.rng.randint(0, 23),
                hours=self.rng.randint(0, 23),
                minutes=self.rng.randint(0, 59),
            )
            submissions.append(
                self.build_submission(
                    profile=profile,
                    problem=problems[index],
                    when=when,
                    result="AC" if solved else self.pick_failure(),
                )
            )
        return submissions

    def build_submission(
        self,
        profile,
        problem,
        when,
        result,
        contest=None,
        contest_problem=None,
        participation=None,
    ):
        language = self.rng.choice(self.languages)
        case_total = float(self.rng.randint(10, 40))
        accepted = result == "AC"
        if result == "CE":
            case_points = 0.0
            status = "CE"
            time = None
            memory = None
        else:
            case_points = case_total if accepted else 0.0
            status = "D"
            time = round(self.rng.uniform(0.01, problem.time_limit), 3)
            memory = float(self.rng.randint(2048, problem.memory_limit))

        submission = Submission.objects.create(
            user=profile,
            problem=problem,
            language=language,
            status=status,
            result=result,
            points=problem.points if accepted else 0.0,
            case_points=case_points,
            case_total=case_total,
            time=time,
            memory=memory,
            current_testcase=int(case_total),
            batch=False,
            judged_date=when + timedelta(seconds=self.rng.randint(1, 20)),
            contest_object=contest,
            error="error: seeded compile failure\n" if result == "CE" else None,
        )
        SubmissionSource.objects.create(
            submission=submission,
            source=SOURCE_SNIPPETS.get(language.key, DEFAULT_SOURCE),
        )
        if participation is not None:
            ContestSubmission.objects.create(
                submission=submission,
                problem=contest_problem,
                participation=participation,
                points=float(contest_problem.points) if accepted else 0.0,
            )
        self.build_test_cases(submission, result, case_total, accepted, time, memory)
        return (submission, when)

    def build_test_cases(self, submission, result, case_total, accepted, time, memory):
        """A handful of test cases so the submission detail page has something to show."""
        if result == "CE":
            return
        cases = min(int(case_total), 8)
        if not cases:
            return
        per_case = case_total / cases
        # All or nothing: a failure fails somewhere in the set, and scores zero.
        failed_case = None if accepted else self.rng.randint(1, cases)
        objects = []
        for case in range(1, cases + 1):
            failed = case == failed_case
            objects.append(
                SubmissionTestCase(
                    submission=submission,
                    case=case,
                    status=result if failed else "AC",
                    time=(
                        None
                        if time is None
                        else round(time * self.rng.uniform(0.3, 1.0), 3)
                    ),
                    memory=memory,
                    points=0.0 if failed_case is not None else round(per_case, 2),
                    total=round(per_case, 2),
                    batch=None,
                    feedback="seeded feedback" if failed else "",
                )
            )
        SubmissionTestCase.objects.bulk_create(objects)

    def apply_dates(self, submissions):
        """`Submission.date` is auto_now_add, so backdate it after insert."""
        objects = []
        for submission, when in submissions:
            submission.date = when
            objects.append(submission)
        Submission.objects.bulk_update(objects, ["date"], batch_size=200)
        if self.verbosity:
            self.stdout.write("Created %d submissions." % len(objects))

    # ------------------------------------------------------------ recomputing

    def recompute(self, contests, problems, profiles, rate=True):
        for contest in contests:
            for participation in contest.users.all():
                participation.recompute_results()
            contest.update_user_count()
        for problem in problems:
            problem.update_stats()
        for profile in profiles:
            profile.calculate_points()
        if rate:
            from judge.ratings import rate_contest

            for contest in sorted(contests, key=lambda c: c.end_time):
                rate_contest(contest)
        if self.verbosity:
            self.stdout.write(
                "Recomputed scoreboards, problem stats, user points%s."
                % (" and ratings" if rate else "")
            )
