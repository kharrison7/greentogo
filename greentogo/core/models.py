from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.auth import hashers
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.signals import user_logged_in
from django.contrib.sites.models import Site
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.mail import EmailMessage
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Case, Count, Q, Sum, When
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.text import slugify

import shortuuid
from django_geocoder.wrapper import get_cached as geocode
from postgres_stats import DateTrunc
from templated_email import send_templated_mail

from core.stripe import stripe
from core.utils import decode_id, encode_nums


def one_year_from_now():
    return timezone.now() + timedelta(days=365)


def activity_data(days=30):
    begin_datetime = timezone.now() - timedelta(days=days)
    begin_datetime_start_of_day = begin_datetime.replace(hour=0, minute=0, second=0)

    def _get_data(qs):
        data = qs.filter(created_at__gte=begin_datetime_start_of_day) \
                 .annotate(date=DateTrunc('created_at', precision='day')) \
                 .values("date") \
                 .annotate(volume=Count("date")) \
                 .order_by("date")
        return [{"date": d['date'].date(), "volume": d['volume']} for d in data]

    checkin_data = _get_data(LocationTag.objects.checkin())
    checkout_data = _get_data(LocationTag.objects.checkout())
    return {"checkin": checkin_data, "checkout": checkout_data}


class User(AbstractUser):
    name = models.CharField(max_length=255, blank=True, null=True)
    email = models.EmailField(
        verbose_name='Email address',
        max_length=255,
        unique=True,
    )
    stripe_id = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return self.name or self.username

    def has_active_subscription(self):
        return self.subscriptions.active().count() > 0

    def create_stripe_customer(self, token=None):
        if self.stripe_id:
            return

        if token is None:
            customer = stripe.Customer.create(
                email=self.email,
            )
        else:
            customer = stripe.Customer.create(
                email=self.email,
                source=token,
            )

        self.stripe_id = customer.id
        self.save()
        return customer

    def get_stripe_customer(self, create=False):
        if not self.stripe_id:
            if create:
                return self.create_stripe_customer()
            else:
                return None

        customer = stripe.Customer.retrieve(self.stripe_id)
        return customer


class CannotChangeException(Exception):
    """Raise when model field should not change after initial creation."""


class PlanQuerySet(models.QuerySet):
    def available(self):
        return self.filter(available=True).exclude(stripe_id=None)

    def as_dicts(self):
        return [plan.as_dict() for plan in self.all()]


class Plan(models.Model):
    objects = PlanQuerySet.as_manager()

    name = models.CharField(max_length=255, unique=True)
    available = models.BooleanField(default=True)
    amount = models.PositiveIntegerField(help_text="Amount in cents.")
    number_of_boxes = models.PositiveIntegerField()
    stripe_id = models.CharField(max_length=255, unique=True, blank=True, null=True, editable=False)

    class Meta:
        ordering = ['amount', 'number_of_boxes']

    @classmethod
    def from_db(cls, db, field_names, values):
        """Overridden in order to allow us to see if a value has changed."""
        instance = super().from_db(db, field_names, values)
        instance._loaded_values = dict(zip(field_names, values))
        return instance

    def __str__(self):
        return "{} - {}".format(self.name, self.stripe_id)

    def as_dict(self):
        return {
            'stripe_id': self.stripe_id,
            'name': self.name,
            'amount': self.amount,
            'display_price': self.display_price(),
            'boxes': self.number_of_boxes
        }

    def display_price(self, corporate_code=None):
        amount = self.amount
        if corporate_code:
            amount -= (corporate_code.amount_off * 100)
        return "${:.02f}".format(amount / 100)

    def is_changed(self, field_name):
        """Find out if a value has changed since it was pulled from the DB."""
        if not hasattr(self, '_loaded_values'):
            return False
        old_value = self._loaded_values[field_name]
        new_value = getattr(self, field_name)
        return not old_value == new_value

    def _generate_stripe_id(self, force=False):
        if force or not self.stripe_id:
            shortuuid.set_alphabet("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
            stripe_id = slugify(self.name) + shortuuid.uuid()[:6]
            while Plan.objects.filter(stripe_id=stripe_id).count() > 0:
                stripe_id = slugify(self.name) + "-" + shortuuid.uuid()[:6]
            return stripe_id

    def save(self, *args, **kwargs):
        if self.stripe_id:
            if self.is_changed('amount'):
                raise CannotChangeException({"model": self, "field": "amount"})
            if self.is_changed('name'):
                stripe_plan = stripe.Plan.retrieve(self.stripe_id)
                stripe_plan.name = self.name
                stripe_plan.save()
        else:
            self.stripe_id = self._generate_stripe_id()
            plan = stripe.Plan.create(
                name=self.name,
                id=self.stripe_id,
                interval="year",
                currency="usd",
                amount=self.amount,
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.available = False
        self.save()


class UnclaimedSubscription(models.Model):
    email = models.EmailField()
    plan = models.ForeignKey(Plan)
    claimed = models.BooleanField(default=False)

    class Meta:
        unique_together = ('email', 'plan', )

    def __str__(self):
        return "{} - {}".format(self.email, self.plan)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def claim_subscriptions(sender, instance, created, **kwargs):
    if created:
        unsubs = UnclaimedSubscription.objects.filter(email__iexact=instance.email, claimed=False)
        for unsub in unsubs:
            subscription, _ = Subscription.objects.get_or_create(
                user=instance, plan=unsub.plan, defaults={"ends_at": one_year_from_now()}
            )
            unsub.claimed = True
            unsub.save()


@receiver(user_logged_in)
def claim_subscriptions_on_login(sender, user, request, **kwargs):
    unsubs = UnclaimedSubscription.objects.filter(email__iexact=user.email, claimed=False)
    for unsub in unsubs:
        subscription, _ = Subscription.objects.get_or_create(
            user=user, plan=unsub.plan, defaults={"ends_at": one_year_from_now()}
        )
        unsub.claimed = True
        unsub.save()


class SubscriptionQuerySet(models.QuerySet):
    def active(self):
        return self.filter(Q(ends_at=None) | Q(ends_at__gt=timezone.now()))

    def owned_by(self, user):
        return self.filter(user=user)


class Subscription(models.Model):
    objects = SubscriptionQuerySet.as_manager()

    name = models.CharField(max_length=255, null=True, blank=True)
    user = models.ForeignKey(User, related_name="subscriptions")
    plan = models.ForeignKey(Plan, null=True)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True)
    stripe_id = models.CharField(max_length=100, blank=True, null=True)
    stripe_status = models.CharField(max_length=100, default="active")
    corporate_code = models.ForeignKey('CorporateCode', null=True, blank=True)

    def __str__(self):
        return "{} - {}".format(self.user.name, self.display_name)

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('subscription', kwargs={"sub_id": self.id})

    @classmethod
    def create_from_stripe_sub(cls, user, plan, stripe_subscription, corporate_code=None):
        ends_at = datetime.fromtimestamp(stripe_subscription.current_period_end) + timedelta(days=3)
        ends_at = timezone.make_aware(ends_at, is_dst=False)
        sub_kwargs = dict(
            user=user,
            stripe_id=stripe_subscription.id,
            plan=plan,
            ends_at=ends_at,
            stripe_status=stripe_subscription.status,
        )

        if corporate_code:
            sub_kwargs['corporate_code'] = corporate_code

        subscription = Subscription.objects.create(**sub_kwargs)
        return subscription

    @classmethod
    def get_from_hashed_id(cls, hashed_id):
        real_id = decode_id(hashed_id)[0]
        return cls.objects.get(id=real_id)

    @property
    def hashed_id(self):
        return encode_nums(self.id)

    @property
    def display_name(self):
        return self.name or self.plan_display()

    def plan_display(self):
        if self.plan:
            return self.plan.name
        return "None"

    def amount_display(self):
        if self.plan:
            return self.plan.display_price(self.corporate_code)

    @property
    def number_of_boxes(self):
        if self.plan:
            return self.plan.number_of_boxes

        return 0

    @property
    def max_boxes(self):
        return self.number_of_boxes

    @property
    def available_boxes(self):
        boxes_checked_out = LocationTag.objects.filter(subscription=self).aggregate(
            checked_out=Sum(
                Case(
                    When(location__service=Location.CHECKOUT, then=1),
                    When(location__service=Location.CHECKIN, then=-1),
                    default=1,
                    output_field=models.IntegerField()
                )
            )
        )['checked_out']
        return self.number_of_boxes - (boxes_checked_out or 0)

    def boxes_checked_out(self):
        return self.number_of_boxes - self.available_boxes

    def amount(self):
        return self.plan.amount

    def one_year_from_start(self):
        return self.starts_at + timedelta(days=365)

    def can_checkout(self, number_of_boxes=1):
        return self.available_boxes >= number_of_boxes

    def can_checkin(self, number_of_boxes=1):
        return self.available_boxes + number_of_boxes <= self.number_of_boxes

    def can_tag_location(self, location, number_of_boxes=1):
        if location.service == Location.CHECKIN:
            return self.can_checkin(number_of_boxes)
        else:
            return self.can_checkout(number_of_boxes)

    def tag_location(self, location, number_of_boxes=1):
        tags = []
        for _ in range(number_of_boxes):
            tags.append(LocationTag.objects.create(subscription=self, location=location))
        return tags

    def will_auto_renew(self):
        return self.has_stripe_subscription() and self.is_stripe_active()

    def is_stripe_active(self):
        return self.stripe_status in ("active", "trialing", )

    def has_stripe_subscription(self):
        return self.stripe_id

    def get_stripe_subscription(self):
        if self.stripe_id is None:
            return None

        return stripe.Subscription.retrieve(self.stripe_id)

    def update_from_stripe_sub(self, stripe_subscription, force=False):
        if self.stripe_id and not force:
            raise SubscriptionUpdateException(
                message="Subscription already has a Stripe subscription",
                subscription=self,
                stripe_subscription=stripe_subscription,
            )

        ends_at = datetime.fromtimestamp(stripe_subscription.current_period_end) + timedelta(days=3)
        ends_at = timezone.make_aware(ends_at, is_dst=False)

        self.stripe_id = stripe_subscription.id
        self.stripe_status = stripe_subscription.status
        self.ends_at = ends_at
        self.save()

    def sync_with_stripe(self, stripe_sub=None):
        if stripe_sub is None:
            stripe_sub = self.get_stripe_subscription()

        if stripe_sub is None:
            return

        self.stripe_id = stripe_sub.id
        self.stripe_status = stripe_sub.status
        if stripe_sub.ended_at:
            self.ends_at = timezone.make_aware(datetime.fromtimestamp(stripe_sub.ended_at))
        else:
            self.ends_at = timezone.make_aware(
                datetime.fromtimestamp(stripe_sub.current_period_end) + timedelta(days=3)
            )
        self.save()


class SubscriptionUpdateException(Exception):
    def __init__(self, subscription, stripe_subscription, message):
        self.subscription = subscription
        self.stripe_subscription = stripe_subscription
        self.message = message


@receiver(post_save, sender=Subscription)
def new_subscription_email_to_admins(sender, instance, created, **kwargs):
    if created:
        send_templated_mail(
            template_name='new_subscription',
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=settings.EMAIL_ADMINS,
            context={'subscription': instance}
        )


class LocationQuerySet(models.QuerySet):
    def checkin(self):
        return self.filter(service=Location.CHECKIN)

    def checkout(self):
        return self.filter(service=Location.CHECKOUT)


class Location(models.Model):
    objects = LocationQuerySet.as_manager()

    CHECKIN = 'IN'
    CHECKOUT = 'OUT'
    SERVICE_CHOICES = ((CHECKIN, 'Check in'), (CHECKOUT, 'Check out'), )

    code = models.CharField(max_length=6, unique=True)
    service = models.CharField(max_length=25, choices=SERVICE_CHOICES)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=1023)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)

    def __str__(self):
        return "{} - {}".format(self.name, self.service)

    def save(self, *args, **kwargs):
        self._set_code()
        self._geocode()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse('location', kwargs={"location_code": self.code})

    def set_stock(self, count):
        return LocationStockCount.objects.create(location=self, count=count)

    def get_estimated_stock(self):
        try:
            last_count = LocationStockCount.objects.filter(location=self).order_by('-created_at')[0]
        except IndexError:
            last_count = LocationStockCount.objects.create(location=self, count=0)

        boxes_moved = LocationTag.objects.filter(
            location=self, created_at__gt=last_count.created_at
        ).count()

        if self.service == self.CHECKOUT:
            return last_count.count - boxes_moved
        else:
            return last_count.count + boxes_moved

    def _set_code(self, force=False):
        if force or not self.code:
            shortuuid.set_alphabet("23456789ABCDEFGHJKLMNPQRSTUVWXYZ")
            code = shortuuid.uuid()[:6]
            while Location.objects.filter(code=code).count() > 0:
                code = shortuuid.uuid()[:6]
            self.code = code

    def _geocode(self):
        if self.address and self.latitude is None or self.longitude is None:
            result = geocode(self.address, provider='google')
            if result.latlng:
                lat, lng = result.latlng
                self.latitude = lat
                self.longitude = lng

    def add_qrcode_to_pdf(self, pdf):
        import qrcode
        from reportlab.lib.units import inch

        width, height = pdf._pagesize

        qr = qrcode.QRCode(box_size=10)
        qr.add_data(settings.URL + self.get_absolute_url())
        qr.make(fit=True)
        img = qr.make_image(fill_color="white", back_color="black")

        image_y = 11 * inch - img.pixel_size
        text_y = image_y + 15
        font_size = 14

        pdf.drawInlineImage(img, x=(width - img.pixel_size) / 2, y=image_y)
        pdf.setFont("Helvetica-Bold", font_size)
        pdf.drawCentredString(width / 2, text_y, self.code)
        pdf.setFont("Helvetica", font_size)
        pdf.drawCentredString(
            width / 2, text_y - (font_size + 2), "{} - {}".format(self.name, self.service)
        )
        pdf.showPage()
        return pdf


class LocationTagQuerySet(models.QuerySet):
    def checkin(self):
        return self.filter(location__service=Location.CHECKIN)

    def checkout(self):
        return self.filter(location__service=Location.CHECKOUT)


class LocationTag(models.Model):
    objects = LocationTagQuerySet.as_manager()

    subscription = models.ForeignKey(Subscription)
    location = models.ForeignKey(Location)
    created_at = models.DateTimeField(auto_now_add=True)


class LocationStockCount(models.Model):
    location = models.ForeignKey(Location, related_name='stock_counts')
    created_at = models.DateTimeField(auto_now_add=True)
    count = models.PositiveIntegerField()


class Restaurant(models.Model):
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=1023)
    latitude = models.FloatField(blank=True, null=True)
    longitude = models.FloatField(blank=True, null=True)
    phase = models.PositiveIntegerField(default=1)

    def save(self, *args, **kwargs):
        if self.address and self.latitude is None or self.longitude is None:
            result = geocode(self.address, provider='google')
            lat, lng = result.latlng
            self.latitude = lat
            self.longitude = lng
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


def one_year_from_now():
    return date.today() + timedelta(days=365)


class CorporateCode(models.Model):
    company_name = models.CharField(max_length=100)
    code = models.CharField(
        max_length=20,
        unique=True,
        validators=[
            RegexValidator(
                regex=r"^[A-Z0-9]{4,20}$",
                message="Code can only contain capitol letters and numbers " +
                "with no spaces. Must be between 4 and 20 characters."
            )
        ]
    )

    amount_off = models.DecimalField(max_digits=5, decimal_places=2, default=25.00)
    redeem_by = models.DateField(default=one_year_from_now)

    def __str__(self):
        return "{} - {}".format(self.company_name, self.code)

    def save(self, *args, **kwargs):
        """
        Disallow editing of codes.
        Create coupon on Stripe upon creation.
        """
        if self.pk is not None:
            return
        coupon = stripe.Coupon.create(
            id=self.code,
            duration="once",
            amount_off=int(self.amount_off * 100),
            currency="USD",
            redeem_by=int(datetime.combine(self.redeem_by, datetime.min.time()).timestamp())
        )
        super().save(*args, **kwargs)
