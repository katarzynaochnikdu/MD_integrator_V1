import sys
try:
    from cryptography.fernet import Fernet
except ImportError:
    print("Błąd: Biblioteka cryptography nie jest zainstalowana.")
    print("Zainstaluj ją wpisując: pip install cryptography")
    sys.exit(1)

def main():
    print("=" * 60)
    print("🔑 Generator kluczy szyfrujących (Fernet) 🔑")
    print("=" * 60)
    
    key = Fernet.generate_key().decode('utf-8')
    
    print("\nTwój nowy, bezpieczny klucz to:")
    print("-" * 60)
    print(f"{key}")
    print("-" * 60)
    print("\nInstrukcja:")
    print("1. Skopiuj DOKŁADNIE cały powyższy tekst (razem z '=' na końcu jeśli jest).")
    print("2. Wejdź do panelu środowiska na Renderze (Environment).")
    print("3. Wklej ten ciąg dla zmiennej MEDIDESK_ENCRYPTION_KEY.")
    print("4. Zapisz zmiany w Renderze.")
    print("=" * 60)

if __name__ == "__main__":
    main()
