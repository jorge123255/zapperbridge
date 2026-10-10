# Releasing (manual)

No CI is used. Run the tests locally, then publish the image by hand.

1. Tests: `python3 -m unittest discover -s tests`
2. Tag: `git tag vX.Y.Z && git push origin vX.Y.Z`
3. Log in to Docker Hub on your own machine (`docker login`). Never put
   credentials in the repo.
4. Build and push a multi-platform image:

```
docker buildx create --use --name zbbuilder   # once
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --provenance=false \
  -t sunnyside1/zapperbridge:vX.Y.Z \
  -t sunnyside1/zapperbridge:latest \
  --push .
```

5. Verify: `docker buildx imagetools inspect sunnyside1/zapperbridge:vX.Y.Z`
6. Create the GitHub release from the tag with release notes.
