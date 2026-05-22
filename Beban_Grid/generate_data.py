import pandas as pd
import random

# Definisi kolom dan batas nilai maksimal (dalam MW)
columns = ['L101', 'L201', 'L301', 'L302', 'L303', 'L304', 'L305', 'L401', 'L402', 'L403', 'L404', 'L405']
max_vals = [20, 20, 5, 10, 15, 20, 30, 30, 5, 10, 15, 20]

data = []
# Membuat 50 baris data
for _ in range(50):
    row = []
    for max_val in max_vals:
        # Menghasilkan nilai acak dari 1 hingga nilai maksimal
        # (Bisa diubah jika ingin ada nilai minimal tertentu, misal minimal 50% dari beban)
        val = random.randint(1, max_val)
        row.append(val)
    data.append(row)

# Menyimpan data ke CSV
df = pd.DataFrame(data, columns=columns)
df.to_csv('beban_grid.csv', index=False)

print("Berhasil membuat 'beban_grid.csv' dengan 50 baris data variatif dan acak!")
