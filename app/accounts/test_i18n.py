from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse


class LanguageSelectionTests(SimpleTestCase):
    """Verify the language preference persists without changing app routes."""

    def test_language_switch_keeps_the_current_path_and_persists_in_cookie(self) -> None:
        login_path = reverse("accounts:login")
        switch_url = reverse("set_language")

        english_response = self.client.post(
            switch_url,
            {"language": "en", "next": login_path},
        )

        self.assertRedirects(english_response, login_path)
        self.assertEqual(
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME].value,
            "en",
        )
        english_page = self.client.get(login_path)
        self.assertContains(english_page, "Sign in to your account")
        self.assertContains(english_page, "User")
        self.assertContains(english_page, "Password")
        self.assertContains(english_page, 'onchange="this.form.requestSubmit()"')
        self.assertNotContains(english_page, "Apply")

        portuguese_response = self.client.post(
            switch_url,
            {"language": "pt-br", "next": login_path},
        )

        self.assertRedirects(portuguese_response, login_path)
        self.assertEqual(
            self.client.cookies[settings.LANGUAGE_COOKIE_NAME].value,
            "pt-br",
        )
        self.assertContains(self.client.get(login_path), "Entrar na sua conta")
