from app.services.udmi_validation import validate_udmi_full_report


def main() -> None:
    result = validate_udmi_full_report({})
    assert result.result_summary["expected_devices"] == 35, result.result_summary
    assert result.result_summary["not_publishing"] == 31, result.result_summary
    assert result.result_summary["issue_count"] == len(result.issues)
    assert result.issues, "Expected normalized issues from the UDMI fixture."
    assert {"issue_id", "asset_id", "issue_type", "severity", "description"} <= set(result.issues[0])
    print(
        "UDMI adapter smoke passed: "
        f"{result.result_summary['issue_count']} normalized issues from {result.source_fixture}."
    )


if __name__ == "__main__":
    main()
