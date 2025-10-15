# Test de traduction

Le but est d'utiliser .po ou .xliff pour générer les traductions de tous les fichiers dans fr/. L'objectif est de traduire avec un llm fr en en.

1. Source de vérité fr/ -> i18n/en/**/*.po
2. Extraction po/xliff avec ids stables
3. Génération de en/ depuis po/xliff
4. Automatisation Git déclenchement quand nécessaire depuis ci
