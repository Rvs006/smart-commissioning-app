from app.tasks import broker


def main() -> None:
    actors = sorted(str(actor) for actor in broker.get_declared_actors())
    print("Registered worker actors:")
    for actor in actors:
        print(f"- {actor}")


if __name__ == "__main__":
    main()
