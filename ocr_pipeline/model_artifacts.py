"""Approved public model revisions and SHA-256 values; no document data."""

MODELS = {
    "PP-OCRv5_server_det": (
        "ca867c897ecbca8873081573a802ad70d499cb94",
        "183146fe9d9910352f68482f623bcbbb9fa7b9e8fa1463b9ad288cef00524d2d",
    ),
    "arabic_PP-OCRv5_mobile_rec": (
        "33d91636a65dca87f5562cc48860332ae367ee1b",
        "4b4271fd1dd89a40b2056e1a42e58de7c0df2fdcf1a97ca9a8916d7ec45f9143",
    ),
}
CONFIG_HASHES = {
    "PP-OCRv5_server_det/inference.json": "af5876933d8806a1b50d895867e0781e135cd92ff37381992828fc8d1b842d28",
    "PP-OCRv5_server_det/inference.yml": "28fb721efc3634fc8aa677e474b9602cb815a91cf569ef357a7a553d7b3ce685",
    "arabic_PP-OCRv5_mobile_rec/inference.json": "d43832804d89c0e18d59f8446a3babd0ef60c44bc335efddac3176caf71d50af",
    "arabic_PP-OCRv5_mobile_rec/inference.yml": "21368419e6c016c31db55d316d59e11c128e1913e6e6fe10287084710043d3a6",
}

EXPECTED_FILES = {
    **CONFIG_HASHES,
    **{f"{name}/inference.pdiparams": values[1] for name, values in MODELS.items()},
}
