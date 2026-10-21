# STATUS 

## BUILT and tested

- conductor
- predicted_path
- use_gate
- reasoning_pipeline
- records
- arsenal (index of tools, repos, symbols, prompts, solutions and skills; search by TF-IDF or a supplied embedding; licence class and use card on repo hits; pipeline stage 12)

Tests: 115 tests in 5 files, all passing.

## DESIGNED ONLY (not built)

- Stages without a supplied dependency report NOT_BUILT (prompt reconstruction, kinetic prediction, history, hybrid; the arsenal stage until something has been indexed).
- The arsenal starts empty; a loader from the repo library, scoring from recorded outcomes, and the self-improvement loop are not built. Matching on meaning needs a supplied embedding function.

## Release

Source-available under the LICENSE in this folder. Patent pending (U.S. provisional application 64/178,949 and others). The licence text is a draft that has not been reviewed by an attorney.
