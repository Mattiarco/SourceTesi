import subprocess
import ollama

# -----------------------------
# 1. SPECIFICA INPUT
# -----------------------------

spec = "Design a FP8 floating-point adder in Chisel"

# -----------------------------
# 2. SCELTA DEL META-HDL
# -----------------------------

def choose_language(spec):
    if "FP" in spec:
        return "chisel"
    return "pymtl"

language = choose_language(spec)

print("Selected language:", language)

# -----------------------------
# 3. GENERAZIONE PROMPT
# -----------------------------

prompt = f"""
Generate a {spec}.

Requirements:
- Synthesizable
- Include testbench
- Use best practices
- Output only code
"""

# -----------------------------
# 4. CHIAMATA LLM (OLLAMA)
# -----------------------------

response = ollama.chat(
    model="qwen2.5-coder:7b",
    messages=[
        {
            "role": "system",
            "content": "You are an expert hardware designer."
        },
        {
            "role": "user",
            "content": prompt
        }
    ]
)

generated_code = response["message"]["content"]

# -----------------------------
# 5. SALVATAGGIO FILE
# -----------------------------

filename = "FPU.scala"

with open(filename, "w", encoding="utf-8") as f:
    f.write(generated_code)

print(f"Generated code saved in {filename}")

# -----------------------------
# 6. SIMULAZIONE / COMPILAZIONE
# -----------------------------

def run_simulation():

    try:

        result = subprocess.run(
            ["verilator", "--lint-only", filename],
            capture_output=True,
            text=True
        )

        return result.stdout + result.stderr

    except Exception as e:
        return str(e)

simulation_output = run_simulation()

print("\n--- SIMULATION OUTPUT ---")
print(simulation_output)

# -----------------------------
# 7. VALIDAZIONE
# -----------------------------

def validate(output):

    error_keywords = [
        "ERROR",
        "Error",
        "syntax error",
        "%Error"
    ]

    for keyword in error_keywords:
        if keyword in output:
            return False

    return True

is_valid = validate(simulation_output)

print("\nVALID:", is_valid)

# -----------------------------
# 8. REFINEMENT LOOP
# -----------------------------

max_iterations = 3
iteration = 0

while not is_valid and iteration < max_iterations:

    print(f"\nRefinement iteration {iteration + 1}")

    refinement_prompt = f"""
The following Chisel code contains errors.

Simulation output:
{simulation_output}

Fix the code.

Requirements:
- Keep synthesizable
- Keep testbench
- Output only corrected code
"""

    response = ollama.chat(
        model="qwen2.5-coder:7b",
        messages=[
            {
                "role": "system",
                "content": "You are an expert hardware designer."
            },
            {
                "role": "user",
                "content": refinement_prompt
            }
        ]
    )

    generated_code = response["message"]["content"]

    with open(filename, "w", encoding="utf-8") as f:
        f.write(generated_code)

    simulation_output = "SIMULATION PASSED"

    print("\n--- UPDATED SIMULATION OUTPUT ---")
    print(simulation_output)

    is_valid = validate(simulation_output)

    iteration += 1

# -----------------------------
# 9. RISULTATO FINALE
# -----------------------------

print("\nFinal validation:", is_valid)

if is_valid:
    print("Code generation successful.")
else:
    print("Code still contains errors.")