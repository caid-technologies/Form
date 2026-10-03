import unittest
from unittest.mock import Mock, patch

from apps.api.opencode_image_data import MAX_IMAGE_BYTES, materialize_image


class ProviderImageDataTests(unittest.TestCase):
    def setUp(self):
        self.resolve = self.enterContext(patch("apps.api.opencode_image_data.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]))
        self.connection = self.enterContext(patch("apps.api.opencode_image_data._PublicHTTPSConnection"))
        self.response = Mock(status=200)
        self.headers = {"Content-Type": "image/png"}
        self.response.getheader.side_effect = self.headers.get
        self.response.read.return_value = b"image"
        self.connection.return_value.getresponse.return_value = self.response

    def test_provider_url_is_downloaded_to_inline_data_without_credentials(self):
        self.assertEqual(("data:image/png;base64,aW1hZ2U=", "image/png"), materialize_image("https://cdn.example/image?token=signed"))
        self.connection.assert_called_once_with("cdn.example", "8.8.8.8")
        self.connection.return_value.request.assert_called_once_with("GET", "/image?token=signed", headers={"User-Agent": "Forma-OSS/1.0", "Accept": "image/png,image/jpeg,image/webp"})
        self.connection.return_value.close.assert_called_once()

    def test_invalid_or_internal_destinations_are_never_downloaded(self):
        for url in ["http://cdn.example/image", "file:///image", "https://user:pass@cdn.example/image", "https://cdn.example:8443/image", "https://cdn.example/image\n"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                materialize_image(url)
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"]:
            self.resolve.return_value = [(2, 1, 6, "", (address, 443))]
            with self.subTest(address=address), self.assertRaises(ValueError):
                materialize_image("https://cdn.example/image")
        self.connection.assert_not_called()

    def test_redirect_destination_is_validated_again(self):
        self.response.status = 302
        self.headers["Location"] = "https://internal.example/image"
        self.resolve.side_effect = [[(2, 1, 6, "", ("8.8.8.8", 443))], [(2, 1, 6, "", ("10.0.0.1", 443))]]
        with self.assertRaises(ValueError):
            materialize_image("https://cdn.example/image")
        self.connection.assert_called_once()
        self.connection.return_value.close.assert_called_once()

    def test_public_redirect_is_downloaded_and_redirect_loops_are_bounded(self):
        redirect = Mock(status=302)
        redirect.getheader.return_value = "/result.png"
        self.connection.return_value.getresponse.side_effect = [redirect, self.response]
        self.assertEqual("image/png", materialize_image("https://cdn.example/image")[1])
        self.connection.return_value.getresponse.side_effect = None
        self.connection.return_value.getresponse.return_value = redirect
        self.connection.reset_mock()
        with self.assertRaises(ValueError):
            materialize_image("https://cdn.example/image")
        self.assertEqual(4, self.connection.call_count)

    def test_remote_payload_type_and_size_are_bounded(self):
        for headers in [{"Content-Type": "text/html"}, {"Content-Type": "image/png", "Content-Length": str(MAX_IMAGE_BYTES + 1)}]:
            self.headers.clear()
            self.headers.update(headers)
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                materialize_image("https://cdn.example/image")
        self.response.read.assert_not_called()
        self.headers.clear()
        self.headers["Content-Type"] = "image/png"
        with patch("apps.api.opencode_image_data.MAX_IMAGE_BYTES", 4), self.assertRaises(ValueError):
            materialize_image("https://cdn.example/image")
        self.response.read.assert_called_once_with(5)

    def test_inline_data_remains_local_and_rejects_invalid_payloads(self):
        self.assertEqual(("data:image/webp;base64,aW1hZ2U=", "image/webp"), materialize_image("data:image/webp;base64,aW1hZ2U="))
        for data in ["data:image/svg+xml;base64,aW1hZ2U=", "data:image/png;base64,", "data:image/png;base64,invalid!", "image/png;base64,aW1hZ2U="]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                materialize_image(data)
        self.resolve.assert_not_called()
        self.connection.assert_not_called()
