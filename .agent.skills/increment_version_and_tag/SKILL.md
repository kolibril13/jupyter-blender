# Increment Version and Tag Skill

## Purpose
This skill automates the process of incrementing the version number in the `blender_manifest.toml` file and creating a new Git tag based on the updated version. The tag is then pushed to the GitHub repository.

## Steps
1. **Increment the Version Number**:
   - Read the `blender_manifest.toml` file.
   - Parse the `version` field.
   - Increment the patch version (e.g., `0.1.0` -> `0.1.1`).
   - Update the file with the new version.

2. **Create and Push Git Tag**:
   - Use the updated version to create a new Git tag in the format `vX.X.X`.
   - Push the tag to the GitHub repository.

## Implementation
The skill will:
- Ensure the `blender_manifest.toml` file exists and is valid.
- Use Git commands to create and push the tag.
- Validate that the tag was successfully pushed.

## Usage
Invoke this skill when you need to:
- Increment the version number in the manifest.
- Create a corresponding Git tag and push it to GitHub.

## Example
```bash
# Increment version and tag
copilot run increment_version_and_tag
```

## Notes
- Ensure you have write access to the repository.
- The skill assumes the repository is clean (no uncommitted changes).